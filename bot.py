import os
import re
import time
import math
import json
import io
import base64
import gzip
import asyncio
import statistics
import datetime as dt
from dataclasses import dataclass
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
from nbt.nbt import NBTFile

# -------------------
# Config / Env
# -------------------
load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
HYPIXEL_API_KEY = os.getenv("HYPIXEL_API_KEY", "")
GUILD_ID = os.getenv("GUILD_ID")  # optional for faster slash sync

if not DISCORD_TOKEN:
    raise SystemExit("Missing DISCORD_TOKEN environment variable.")

# Hypixel endpoints
HYPIXEL_AUCTIONS_URL = "https://api.hypixel.net/skyblock/auctions"
HYPIXEL_BAZAAR_URL = "https://api.hypixel.net/skyblock/bazaar"
HYPIXEL_PROFILES_URL = "https://api.hypixel.net/skyblock/profiles"
MOJANG_UUID_URL = "https://api.mojang.com/users/profiles/minecraft/{username}"

START_TIME = time.time()

# -------------------
# Small helpers
# -------------------
def human_int(n: int) -> str:
    return f"{n:,}"

def coins_fmt(n: int) -> str:
    return f"{human_int(int(n))} coins"

def normalize(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip()).lower()

def parse_minecraft_json_text(s: str) -> str:
    # Hypixel item display names can be JSON chat components or plain strings
    try:
        obj = json.loads(s)
        if isinstance(obj, dict) and "text" in obj:
            return str(obj["text"])
        if isinstance(obj, list):
            parts = []
            for it in obj:
                if isinstance(it, dict) and "text" in it:
                    parts.append(str(it["text"]))
            out = "".join(parts).strip()
            return out if out else s
    except Exception:
        pass
    return s

def safe_eval(expr: str) -> float:
    """
    Safe calculator: supports + - * / ** % ( ) and a few math functions.
    """
    import ast
    allowed_names = {
        "pi": math.pi,
        "e": math.e,
        "sqrt": math.sqrt,
        "sin": math.sin,
        "cos": math.cos,
        "tan": math.tan,
        "log": math.log,
        "log10": math.log10,
        "abs": abs,
        "round": round,
        "floor": math.floor,
        "ceil": math.ceil,
        "pow": pow,
    }
    allowed_nodes = (
        ast.Expression, ast.BinOp, ast.UnaryOp, ast.Num, ast.Constant,
        ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow, ast.Mod,
        ast.USub, ast.UAdd,
        ast.Call, ast.Name, ast.Load,
        ast.Tuple,
    )
    tree = ast.parse(expr, mode="eval")
    for node in ast.walk(tree):
        if not isinstance(node, allowed_nodes):
            raise ValueError("Unsupported expression.")
        if isinstance(node, ast.Name) and node.id not in allowed_names:
            raise ValueError(f"Name '{node.id}' not allowed.")
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in allowed_names:
                raise ValueError("Only whitelisted functions are allowed.")
    code = compile(tree, "<calc>", "eval")
    return float(eval(code, {"__builtins__": {}}, allowed_names))

# -------------------
# Hypixel client
# -------------------
@dataclass
class AuctionHit:
    item_name: str
    starting_bid: int
    auctioneer: str
    end: int

class HypixelClient:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.session: Optional[aiohttp.ClientSession] = None

    async def start(self):
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=25))

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()

    async def fetch_json(self, url: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if not self.session:
            await self.start()
        headers = {}
        if self.api_key:
            headers["API-Key"] = self.api_key

        async with self.session.get(url, params=params, headers=headers) as r:
            data = await r.json(content_type=None)
            if r.status != 200 or not data.get("success", False):
                raise RuntimeError(f"API error ({r.status}): {data.get('cause', 'unknown')}")
            return data

    async def search_lowest_bin(self, item_query: str, max_pages: int = 3, max_hits: int = 300) -> Tuple[Optional[AuctionHit], List[int], int]:
        q = normalize(item_query)
        prices: List[int] = []
        best: Optional[AuctionHit] = None
        pages_scanned = 0

        first = await self.fetch_json(HYPIXEL_AUCTIONS_URL, params={"page": 0})
        total_pages = int(first.get("totalPages", 0) or 0)
        pages_to_scan = min(max_pages, total_pages if total_pages > 0 else max_pages)

        async def scan(payload: Dict[str, Any]) -> bool:
            nonlocal best
            for a in payload.get("auctions", []) or []:
                if not bool(a.get("bin", False)):
                    continue
                name = a.get("item_name") or ""
                if q not in normalize(name):
                    continue
                price = int(a.get("starting_bid", 0) or 0)
                if price <= 0:
                    continue
                prices.append(price)
                if best is None or price < best.starting_bid:
                    best = AuctionHit(
                        item_name=name,
                        starting_bid=price,
                        auctioneer=str(a.get("auctioneer", "")),
                        end=int(a.get("end", 0) or 0),
                    )
                if len(prices) >= max_hits:
                    return True
            return False

        stop = await scan(first)
        pages_scanned += 1
        if stop:
            return best, prices, pages_scanned

        for page in range(1, pages_to_scan):
            payload = await self.fetch_json(HYPIXEL_AUCTIONS_URL, params={"page": page})
            pages_scanned += 1
            stop = await scan(payload)
            if stop:
                break

        return best, prices, pages_scanned

hypixel = HypixelClient(HYPIXEL_API_KEY)

# -------------------
# Networth decoding + pricing (best-effort)
# -------------------
def decode_inventory_items(data_b64: str) -> List[Any]:
    raw = base64.b64decode(data_b64)
    unzipped = gzip.decompress(raw)
    nbt = NBTFile(fileobj=io.BytesIO(unzipped))
    items = []
    if "i" in nbt:
        for comp in nbt["i"]:
            items.append(comp)
    return items

def extract_item_id_name_count(item_comp) -> Tuple[Optional[str], Optional[str], int]:
    # Count
    try:
        count = int(item_comp.get("Count", 1).value)
    except Exception:
        count = 1

    sky_id = None
    disp = None
    try:
        tag = item_comp["tag"]
        if "ExtraAttributes" in tag and "id" in tag["ExtraAttributes"]:
            sky_id = str(tag["ExtraAttributes"]["id"].value)
        if "display" in tag and "Name" in tag["display"]:
            disp = parse_minecraft_json_text(str(tag["display"]["Name"].value))
    except Exception:
        pass

    return sky_id, disp, count

class PriceCache:
    def __init__(self):
        self._bazaar: Optional[Dict[str, Any]] = None
        self._bazaar_ts = 0.0
        self._bin_cache: Dict[str, Tuple[int, float]] = {}

    async def get_bazaar(self, ttl_s: int = 120) -> Dict[str, Any]:
        now = time.time()
        if self._bazaar and (now - self._bazaar_ts) < ttl_s:
            return self._bazaar
        data = await hypixel.fetch_json(HYPIXEL_BAZAAR_URL)
        self._bazaar = data
        self._bazaar_ts = now
        return data

    def get_bin(self, key: str, ttl_s: int = 300) -> Optional[int]:
        hit = self._bin_cache.get(key.lower())
        if not hit:
            return None
        price, ts = hit
        if (time.time() - ts) > ttl_s:
            return None
        return price

    def set_bin(self, key: str, price: int):
        self._bin_cache[key.lower()] = (price, time.time())

price_cache = PriceCache()

async def mojang_username_to_uuid(username: str) -> str:
    if not hypixel.session:
        await hypixel.start()
    url = MOJANG_UUID_URL.format(username=username)
    async with hypixel.session.get(url) as r:
        if r.status == 204:
            raise ValueError("Username not found.")
        data = await r.json(content_type=None)
        undashed = data.get("id")
        if not undashed:
            raise ValueError("Could not resolve UUID.")
        return undashed

def pick_most_recent_profile(profiles_payload: Dict[str, Any], uuid_undashed: str) -> Optional[Dict[str, Any]]:
    best = None
    best_ts = -1
    for p in (profiles_payload.get("profiles") or []):
        members = p.get("members") or {}
        m = members.get(uuid_undashed)
        if not m:
            continue
        ts = int(m.get("last_save", 0) or 0)
        if ts > best_ts:
            best_ts = ts
            best = p
    return best

async def compute_networth(profile: Dict[str, Any], uuid_undashed: str) -> Tuple[int, Dict[str, Any]]:
    members = profile.get("members") or {}
    m = members.get(uuid_undashed) or {}

    purse = int(m.get("coin_purse", 0) or 0)
    bank = int(((profile.get("banking") or {}).get("balance", 0)) or 0)

    bazaar = await price_cache.get_bazaar()
    products = (bazaar.get("products") or {})

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

    items: List[Tuple[Optional[str], Optional[str], int]] = []
    for k in inv_keys:
        block = m.get(k)
        if not isinstance(block, dict):
            continue
        data_b64 = block.get("data")
        if not data_b64:
            continue
        try:
            for comp in decode_inventory_items(data_b64):
                sky_id, disp, count = extract_item_id_name_count(comp)
                if not sky_id and not disp:
                    continue
                items.append((sky_id, disp, count))
        except Exception:
            continue

    valued = 0
    detail = []
    skipped = defaultdict(int)

    # Bazaar-priced items first
    non_bazaar_names: List[str] = []
    for sky_id, disp, count in items:
        if sky_id and sky_id in products:
            sell_price = float(products[sky_id]["quick_status"]["sellPrice"])
            item_val = int(sell_price * count)
            valued += item_val
            detail.append((disp or sky_id, count, item_val, "bazaar"))
        else:
            if disp:
                non_bazaar_names.append(disp)
            else:
                skipped[sky_id or "UNKNOWN"] += count

    # Limited BIN pricing for non-bazaar items (to avoid rate limits)
    unique = []
    seen = set()
    for n in non_bazaar_names:
        key = n.lower()
        if key not in seen:
            seen.add(key)
            unique.append(n)

    MAX_BIN_LOOKUPS = 12
    for name in unique[:MAX_BIN_LOOKUPS]:
        cached = price_cache.get_bin(name)
        if cached is not None:
            price = cached
        else:
            best, _, _ = await hypixel.search_lowest_bin(name, max_pages=3, max_hits=250)
            price = best.starting_bid if best else 0
            if price > 0:
                price_cache.set_bin(name, price)

        total_count = sum(c for _, disp, c in items if disp == name)
        if price > 0 and total_count > 0:
            item_val = int(price * total_count)
            valued += item_val
            detail.append((name, total_count, item_val, "bin"))
        else:
            skipped[name] += total_count

    total = purse + bank + valued
    breakdown = {
        "profile_name": profile.get("cute_name") or "Unknown",
        "purse": purse,
        "bank": bank,
        "valued_items": valued,
        "detail": detail,
        "skipped": dict(skipped),
        "items_count": len(items),
    }
    return total, breakdown

# -------------------
# Discord bot setup
# -------------------
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

async def sync_commands():
    if GUILD_ID:
        guild = discord.Object(id=int(GUILD_ID))
        bot.tree.copy_global_to(guild=guild)
        await bot.tree.sync(guild=guild)
    else:
        await bot.tree.sync()

@bot.event
async def on_ready():
    await hypixel.start()
    try:
        await sync_commands()
    except Exception as e:
        print("Slash sync failed:", e)

    print(f"Logged in as {bot.user} (ID: {bot.user.id})")

# -------------------
# Slash commands
# -------------------
@bot.tree.command(name="status", description="Bot status: ping + uptime")
async def status_cmd(interaction: discord.Interaction):
    uptime_s = int(time.time() - START_TIME)
    uptime = str(dt.timedelta(seconds=uptime_s))
    latency_ms = int(bot.latency * 1000)

    embed = discord.Embed(title="✅ Status", timestamp=dt.datetime.utcnow())
    embed.add_field(name="Ping", value=f"{latency_ms} ms", inline=True)
    embed.add_field(name="Uptime", value=uptime, inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="calc", description="Safe calculator (e.g. 2*(3+4), sqrt(144), sin(pi/2))")
@app_commands.describe(expression="Math expression")
async def calc_cmd(interaction: discord.Interaction, expression: str):
    try:
        result = safe_eval(expression)
        out = str(int(round(result))) if abs(result - round(result)) < 1e-12 else str(result)
        await interaction.response.send_message(f"🧮 `{expression}` = **{out}**", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ Couldn’t compute that: `{e}`", ephemeral=True)

@bot.tree.command(name="timer", description="Set a timer and get pinged when it ends")
@app_commands.describe(seconds="How many seconds", note="Optional message")
async def timer_cmd(interaction: discord.Interaction, seconds: int, note: Optional[str] = None):
    if seconds <= 0 or seconds > 86400:
        await interaction.response.send_message("❌ Timer must be between 1 and 86400 seconds.", ephemeral=True)
        return

    await interaction.response.send_message(f"⏳ Timer set for **{seconds}s**.", ephemeral=True)
    channel_id = interaction.channel_id
    user_id = interaction.user.id
    note_txt = f" — {note}" if note else ""

    async def fire():
        await asyncio.sleep(seconds)
        channel = bot.get_channel(channel_id)
        if channel:
            await channel.send(f"⏰ <@{user_id}> timer done ({seconds}s){note_txt}")

    bot.loop.create_task(fire())

@bot.tree.command(name="auction", description="Search auctions: lowest BIN for an item + quick stats")
@app_commands.describe(item="Item name (partial match)", pages="How many pages to scan (1-10)")
async def auction_cmd(interaction: discord.Interaction, item: str, pages: int = 3):
    pages = max(1, min(pages, 10))
    await interaction.response.defer(ephemeral=True)

    try:
        best, prices, scanned = await hypixel.search_lowest_bin(item, max_pages=pages, max_hits=350)
    except Exception as e:
        await interaction.followup.send(f"❌ Auction lookup failed: `{e}`", ephemeral=True)
        return

    if not prices:
        await interaction.followup.send(f"😕 No BIN auctions found for **{item}** (scanned {scanned} page(s)).", ephemeral=True)
        return

    prices_sorted = sorted(prices)
    median = int(statistics.median(prices_sorted))
    p10 = prices_sorted[max(0, int(len(prices_sorted) * 0.10) - 1)]
    p90 = prices_sorted[min(len(prices_sorted) - 1, int(len(prices_sorted) * 0.90))]

    embed = discord.Embed(
        title="🏷️ Auction Search (BIN)",
        description=f"Query: **{item}**\nMatches: **{len(prices)}** | Pages scanned: **{scanned}**",
        timestamp=dt.datetime.utcnow(),
    )

    if best:
        ends_in_s = max(0, int((best.end / 1000) - time.time()))
        embed.add_field(
            name="Lowest BIN",
            value=f"**{best.item_name}**\n{coins_fmt(best.starting_bid)}\nEnds in ~{ends_in_s}s",
            inline=False,
        )

    embed.add_field(name="Median (rough)", value=coins_fmt(median), inline=True)
    embed.add_field(name="Range (p10 → p90)", value=f"{coins_fmt(p10)} → {coins_fmt(p90)}", inline=True)

    await interaction.followup.send(embed=embed, ephemeral=True)

@bot.tree.command(name="crystalpc", description="Crystal price check based on current BIN auctions")
@app_commands.describe(crystal="Crystal name (e.g. Jasper, Ruby, Amethyst)", pages="Pages to scan (1-10)")
async def crystalpc_cmd(interaction: discord.Interaction, crystal: str, pages: int = 5):
    pages = max(1, min(pages, 10))
    await interaction.response.defer(ephemeral=True)

    q = crystal.strip()
    if "crystal" not in q.lower():
        q = f"{q} crystal"

    try:
        best, prices, scanned = await hypixel.search_lowest_bin(q, max_pages=pages, max_hits=450)
    except Exception as e:
        await interaction.followup.send(f"❌ Crystal price check failed: `{e}`", ephemeral=True)
        return

    if not best or not prices:
        await interaction.followup.send(f"😕 No BIN auctions found for **{q}** (scanned {scanned} page(s)).", ephemeral=True)
        return

    prices_sorted = sorted(prices)
    sample = prices_sorted[: min(30, len(prices_sorted))]
    typical = int(statistics.median(sample))

    embed = discord.Embed(
        title="💎 Crystal Price Check (BIN)",
        description=f"Search: **{q}**\nMatches: **{len(prices)}** | Pages scanned: **{scanned}**",
        timestamp=dt.datetime.utcnow(),
    )
    embed.add_field(name="Lowest BIN now", value=f"**{best.item_name}**\n{coins_fmt(best.starting_bid)}", inline=False)
    embed.add_field(name="Typical (median of cheapest sample)", value=coins_fmt(typical), inline=True)
    embed.add_field(name="Cheapest sample size", value=str(len(sample)), inline=True)

    await interaction.followup.send(embed=embed, ephemeral=True)

@bot.tree.command(name="networth", description="Full networth (best-effort): inventories + bazaar + limited BIN")
@app_commands.describe(player="Minecraft username")
async def networth_cmd(interaction: discord.Interaction, player: str):
    await interaction.response.defer(ephemeral=True)

    if not HYPIXEL_API_KEY:
        await interaction.followup.send("❌ Missing `HYPIXEL_API_KEY` (Railway Variables).", ephemeral=True)
        return

    try:
        uuid_undashed = await mojang_username_to_uuid(player)
    except Exception as e:
        await interaction.followup.send(f"❌ Couldn’t resolve UUID for `{player}`: `{e}`", ephemeral=True)
        return

    try:
        profiles_payload = await hypixel.fetch_json(HYPIXEL_PROFILES_URL, params={"uuid": uuid_undashed})
    except Exception as e:
        await interaction.followup.send(f"❌ Hypixel profiles lookup failed: `{e}`", ephemeral=True)
        return

    profile = pick_most_recent_profile(profiles_payload, uuid_undashed)
    if not profile:
        await interaction.followup.send("😕 No SkyBlock profile found (or API disabled).", ephemeral=True)
        return

    try:
        total, b = await compute_networth(profile, uuid_undashed)
    except Exception as e:
        await interaction.followup.send(f"❌ Networth compute failed: `{e}`", ephemeral=True)
        return

    detail = b["detail"]
    top_lines = sorted(detail, key=lambda x: x[2], reverse=True)[:6]
    skipped_count = len(b["skipped"])

    embed = discord.Embed(
        title="🧾 Networth (best-effort)",
        description=(
            f"Player: **{player}**\n"
            f"Profile: **{b['profile_name']}**\n"
            f"Items read: **{b['items_count']}**\n\n"
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
            value=f"{skipped_count} unique item(s) couldn’t be priced this run (missing bazaar id / no BIN / lookup limit).",
            inline=False,
        )

    embed.set_footer(text="Tip: Player must enable SkyBlock API 'Inventory' for full inventory networth.")
    await interaction.followup.send(embed=embed, ephemeral=True)

# -------------------
# Run
# -------------------
bot.run(DISCORD_TOKEN)
