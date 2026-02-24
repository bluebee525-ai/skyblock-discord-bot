import base64
import gzip
import io
import json
from collections import defaultdict
from typing import Optional

import aiohttp
from nbt.nbt import NBTFile  # pip install nbt


# --- add endpoints ---
MOJANG_UUID_URL = "https://api.mojang.com/users/profiles/minecraft/{username}"
HYPIXEL_PROFILES_URL = "https://api.hypixel.net/skyblock/profiles"
HYPIXEL_BAZAAR_URL = "https://api.hypixel.net/skyblock/bazaar"


def parse_minecraft_json_text(s: str) -> str:
    """
    item display name is often stored as JSON string like {"text":"Aspect of...","color":"gold"}
    sometimes it's plain text. We'll try JSON parse then fallback.
    """
    try:
        obj = json.loads(s)
        if isinstance(obj, dict) and "text" in obj:
            return str(obj["text"])
        # Sometimes it's a list of components
        if isinstance(obj, list):
            # best-effort concat
            parts = []
            for it in obj:
                if isinstance(it, dict) and "text" in it:
                    parts.append(str(it["text"]))
            out = "".join(parts).strip()
            return out if out else s
    except Exception:
        pass
    return s


def decode_inventory_base64_gzip_nbt(data_b64: str) -> list[dict]:
    """
    Hypixel inventories are base64 encoded gzipped NBT. :contentReference[oaicite:7]{index=7}
    Returns list of item compounds (as python dict-ish via NBT tags).
    """
    raw = base64.b64decode(data_b64)
    unzipped = gzip.decompress(raw)
    nbt = NBTFile(fileobj=io.BytesIO(unzipped))
    # In Hypixel inventories, items are typically under root tag "i" (list).
    items = []
    if "i" in nbt:
        for comp in nbt["i"]:
            # empty slots can show up as None-ish / missing id
            try:
                items.append(comp)
            except Exception:
                continue
    return items


def extract_skyblock_id_and_name(item_comp) -> tuple[str | None, str | None, int]:
    """
    Reads NBT item compound for:
    - Count
    - tag.ExtraAttributes.id (SkyBlock internal ID)
    - tag.display.Name (pretty name)
    """
    try:
        count = int(item_comp.get("Count", 1).value)
    except Exception:
        count = 1

    skyblock_id = None
    display_name = None

    try:
        tag = item_comp["tag"]
        # SkyBlock ID
        if "ExtraAttributes" in tag and "id" in tag["ExtraAttributes"]:
            skyblock_id = str(tag["ExtraAttributes"]["id"].value)

        # Display name
        if "display" in tag and "Name" in tag["display"]:
            display_name = parse_minecraft_json_text(str(tag["display"]["Name"].value))
    except Exception:
        pass

    return skyblock_id, display_name, count


class PriceCache:
    def __init__(self):
        self._bazaar = None
        self._bazaar_ts = 0.0

        self._bin_cache: dict[str, tuple[int, float]] = {}  # name -> (price, ts)

    async def get_bazaar(self, hypixel_client, ttl_s: int = 120) -> dict:
        now = time.time()
        if self._bazaar and (now - self._bazaar_ts) < ttl_s:
            return self._bazaar
        data = await hypixel_client.fetch_json(HYPIXEL_BAZAAR_URL, params=None)
        self._bazaar = data
        self._bazaar_ts = now
        return data

    def get_cached_bin(self, key: str, ttl_s: int = 300) -> int | None:
        hit = self._bin_cache.get(key)
        if not hit:
            return None
        price, ts = hit
        if (time.time() - ts) > ttl_s:
            return None
        return price

    def set_cached_bin(self, key: str, price: int):
        self._bin_cache[key] = (price, time.time())


price_cache = PriceCache()


async def mojang_username_to_uuid(session: aiohttp.ClientSession, username: str) -> str:
    """
    Mojang endpoint returns {"id":"undasheduuid","name":"..."} for valid usernames. :contentReference[oaicite:8]{index=8}
    """
    url = MOJANG_UUID_URL.format(username=username)
    async with session.get(url) as r:
        if r.status == 204:
            raise ValueError("Username not found.")
        data = await r.json(content_type=None)
        undashed = data.get("id")
        if not undashed:
            raise ValueError("Could not resolve UUID.")
        return undashed


def pick_most_recent_profile(profiles_payload: dict, member_uuid_undashed: str) -> dict | None:
    """
    /skyblock/profiles returns list of profiles; we pick the one with greatest members[uuid].last_save. :contentReference[oaicite:9]{index=9}
    """
    best = None
    best_ts = -1
    for p in profiles_payload.get("profiles", []) or []:
        members = p.get("members", {}) or {}
        m = members.get(member_uuid_undashed)
        if not m:
            continue
        ts = int(m.get("last_save", 0) or 0)
        if ts > best_ts:
            best_ts = ts
            best = p
    return best


async def compute_networth_for_member(hypixel_client, profile: dict, member_uuid_undashed: str) -> tuple[int, dict]:
    """
    Returns (total_coins, breakdown).
    Breakdown includes purse/bank + valued_items + skipped_items.
    """
    members = profile.get("members", {}) or {}
    m = members.get(member_uuid_undashed) or {}

    purse = int(m.get("coin_purse", 0) or 0)
    bank = int(((profile.get("banking") or {}).get("balance", 0)) or 0)

    # Fetch bazaar prices
    bazaar = await price_cache.get_bazaar(hypixel_client)
    products = (bazaar.get("products") or {})

    valued = 0
    valued_items_detail = []
    skipped_items = defaultdict(int)

    # Inventories to include (common ones; more exist)
    inv_keys = [
        "inv_contents",
        "ender_chest_contents",
        "inv_armor",
        "wardrobe_contents",
        "talisman_bag",
        "potion_bag",
        "quiver",
        "fishing_bag",
    ]

    # Collect items (SkyBlock ID + display name + count)
    # Some inventories might be missing if API settings are off. :contentReference[oaicite:10]{index=10}
    items_collected: list[tuple[str | None, str | None, int]] = []

    for k in inv_keys:
        block = m.get(k)
        if not block or not isinstance(block, dict):
            continue
        data_b64 = block.get("data")
        if not data_b64:
            continue

        try:
            comps = decode_inventory_base64_gzip_nbt(data_b64)
            for comp in comps:
                sky_id, disp, count = extract_skyblock_id_and_name(comp)
                if sky_id is None and disp is None:
                    continue
                items_collected.append((sky_id, disp, count))
        except Exception:
            # If one inventory fails, keep going
            continue

    # First pass: value bazaar items using quick_status.sellPrice :contentReference[oaicite:11]{index=11}
    non_bazaar_names: list[str] = []
    for sky_id, disp, count in items_collected:
        if sky_id and sky_id in products:
            sell_price = products[sky_id]["quick_status"]["sellPrice"]
            item_val = int(sell_price * count)
            valued += item_val
            valued_items_detail.append((disp or sky_id, count, item_val, "bazaar"))
        else:
            # try BIN later using display name (best effort)
            if disp:
                non_bazaar_names.append(disp)
            else:
                skipped_items[sky_id or "UNKNOWN"] += count

    # Second pass (best-effort): lowest BIN for a limited number of unique names,
    # to avoid hammering the API (Hypixel has strict request limits). :contentReference[oaicite:12]{index=12}
    # Note: auctions endpoint doesn't require a key; still rate limited overall. :contentReference[oaicite:13]{index=13}
    unique_names = []
    seen = set()
    for n in non_bazaar_names:
        key = n.lower()
        if key not in seen:
            seen.add(key)
            unique_names.append(n)

    MAX_BIN_LOOKUPS_PER_RUN = 12  # keep small to be safe
    for n in unique_names[:MAX_BIN_LOOKUPS_PER_RUN]:
        cached = price_cache.get_cached_bin(n)
        if cached is not None:
            price = cached
        else:
            best, prices, _ = await hypixel_client.search_lowest_bin(item_query=n, max_pages=3, max_hits=200)
            price = best.starting_bid if best else 0
            if price > 0:
                price_cache.set_cached_bin(n, price)

        if price > 0:
            # count how many of this display name we have
            total_count = sum(c for _, disp, c in items_collected if disp == n)
            item_val = int(price * total_count)
            valued += item_val
            valued_items_detail.append((n, total_count, item_val, "bin"))
        else:
            skipped_items[n] += sum(c for _, disp, c in items_collected if disp == n)

    total = purse + bank + valued
    breakdown = {
        "purse": purse,
        "bank": bank,
        "valued_items": valued,
        "valued_items_detail": valued_items_detail,
        "skipped_items": dict(skipped_items),
        "profile_name": profile.get("cute_name") or "Unknown",
    }
    return total, breakdown


# --- replace your existing /networth with this one ---
@bot.tree.command(name="networth", description="Full networth (best-effort): inventories + bazaar + lowest BIN")
@app_commands.describe(player="Minecraft username")
async def networth_cmd(interaction: discord.Interaction, player: str):
    await interaction.response.defer(ephemeral=True)

    if not HYPIXEL_API_KEY:
        await interaction.followup.send("❌ Missing `HYPIXEL_API_KEY` in .env", ephemeral=True)
        return

    # Use the same aiohttp session from hypixel client if you want;
    # simplest is to reuse hypixel.session which we already created.
    if not hypixel.session:
        await hypixel.start()

    try:
        uuid_undashed = await mojang_username_to_uuid(hypixel.session, player)
    except Exception as e:
        await interaction.followup.send(f"❌ Couldn’t resolve UUID for `{player}`: `{e}`", ephemeral=True)
        return

    # Fetch all profiles for that UUID :contentReference[oaicite:14]{index=14}
    try:
        profiles_payload = await hypixel.fetch_json(HYPIXEL_PROFILES_URL, params={"uuid": uuid_undashed})
    except Exception as e:
        await interaction.followup.send(f"❌ Hypixel profiles lookup failed: `{e}`", ephemeral=True)
        return

    profile = pick_most_recent_profile(profiles_payload, uuid_undashed)
    if not profile:
        await interaction.followup.send("😕 No SkyBlock profile data found (or API disabled).", ephemeral=True)
        return

    try:
        total, b = await compute_networth_for_member(hypixel, profile, uuid_undashed)
    except Exception as e:
        await interaction.followup.send(f"❌ Networth compute failed: `{e}`", ephemeral=True)
        return

    skipped_count = len(b["skipped_items"])
    detail = b["valued_items_detail"]
    top_lines = sorted(detail, key=lambda x: x[2], reverse=True)[:6]

    embed = discord.Embed(
        title="🧾 Networth (best-effort)",
        description=(
            f"Player: **{player}**\n"
            f"Profile: **{b['profile_name']}**\n\n"
            f"**Total:** {coins_fmt(total)}\n"
            f"Purse: {coins_fmt(b['purse'])}\n"
            f"Bank: {coins_fmt(b['bank'])}\n"
            f"Valued Items: {coins_fmt(b['valued_items'])}\n"
        ),
        timestamp=dt.datetime.utcnow(),
    )

    if top_lines:
        pretty = "\n".join([f"- {name} x{cnt}: {coins_fmt(val)} ({src})" for (name, cnt, val, src) in top_lines])
        embed.add_field(name="Top valued items (sample)", value=pretty, inline=False)

    if skipped_count > 0:
        embed.add_field(
            name="Unpriced / skipped items",
            value=f"{skipped_count} unique item(s) couldn’t be priced this run (API limits / missing BIN / no bazaar id).",
            inline=False,
        )

    embed.set_footer(text="Tip: Inventories require SkyBlock API: Inventory enabled in-game.")
    await interaction.followup.send(embed=embed, ephemeral=True)
