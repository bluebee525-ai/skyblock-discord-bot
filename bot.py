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

# --------------------
# Env / Config
# --------------------
load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
HYPIXEL_API_KEY = os.getenv("HYPIXEL_API_KEY", "")
GUILD_ID = os.getenv("GUILD_ID")  # optional

if not DISCORD_TOKEN:
    raise SystemExit("Missing DISCORD_TOKEN env var.")
if not HYPIXEL_API_KEY:
    print("⚠️ HYPIXEL_API_KEY missing. /networth will not work.")

START_TIME = time.time()

# Hypixel v2 endpoints
HYPIXEL_V2_SKYBLOCK_PROFILES = "https://api.hypixel.net/v2/skyblock/profiles"
HYPIXEL_V2_SKYBLOCK_AUCTIONS = "https://api.hypixel.net/v2/skyblock/auctions"
HYPIXEL_BAZAAR = "https://api.hypixel.net/skyblock/bazaar"

# Mojang UUID lookup
MOJANG_UUID_URL = "https://api.mojang.com/users/profiles/minecraft/{username}"

# Hypixel status page (HTML)
HYPIXEL_STATUS_PAGE = "https://status.hypixel.net/"

# DuckDuckGo Instant Answer API (free)
DDG_INSTANT = "https://api.duckduckgo.com/"

# --------------------
# Helpers
# --------------------
def human_int(n: int) -> str:
    return f"{int(n):,}"

def coins_fmt(n: int) -> str:
    return f"{human_int(n)} coins"

def normalize(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip()).lower()

def safe_eval(expr: str) -> float:
    """Safe calculator."""
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

def parse_mc_json_text(s: str) -> str:
    """Item display names can be JSON components or plain text."""
    try:
        obj = json.loads(s)
        if isinstance(obj, dict) and "text" in obj:
            return str(obj["text"])
        if isinstance(obj, list):
            out = []
            for it in obj:
                if isinstance(it, dict) and "text" in it:
                    out.append(str(it["text"]))
            joined = "".join(out).strip()
            return joined if joined else s
    except Exception:
        pass
    return s

# --------------------
# HTTP Client
# --------------------
class Http:
    def __init__(self):
        self.session: Optional[aiohttp.ClientSession] = None

    async def start(self):
        if not self.session or self.session.closed:
            self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=25))

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()

    async def get_json(self, url: str, params: Optional[Dict[str, Any]] = None, headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        await self.start()
        assert self.session
        async with self.session.get(url, params=params, headers=headers) as r:
            data = await r.json(content_type=None)
            return data

    async def get_text(self, url: str, params: Optional[Dict[str, Any]] = None, headers: Optional[Dict[str, str]] = None) -> str:
        await self.start()
        assert self.session
        async with self.session.get(url, params=params, headers=headers) as r:
            return await r.text()

http = Http()

def hypixel_headers() -> Dict[str, str]:
    return {"API-Key": HYPIXEL_API_KEY} if HYPIXEL_API_KEY else {}

# --------------------
# Auctions (v2)
# --------------------
@dataclass
class AuctionHit:
    item_name: str
    starting_bid: int
    end: int

async def lowest_bin(item_query: str, max_pages: int = 3, max_hits: int = 350) -> Tuple[Optional[AuctionHit], List[int], int]:
    q = normalize(item_query)
    prices: List[int] = []
    best: Optional[AuctionHit] = None
    pages_scanned = 0

    first = await http.get_json(HYPIXEL_V2_SKYBLOCK_AUCTIONS, params={"page": 0})
    if not first.get("success", False):
        raise RuntimeError(first.get("cause", "Unknown auctions error"))

    total_pages = int(first.get("totalPages", 0) or 0)
    pages_to_scan = min(max_pages, total_pages if total_pages > 0 else max_pages)

    async def scan(payload: Dict[str, Any]) -> bool:
        nonlocal best
        for a in payload.get("auctions", []) or []:
            # BIN field may be missing when false; treat missing as False
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
                best = AuctionHit(item_name=name, starting_bid=price, end=int(a.get("end", 0) or 0))

            if len(prices) >= max_hits:
                return True
        return False

    stop = await scan(first)
    pages_scanned += 1
    if stop:
        return best, prices, pages_scanned

    for page in range(1, pages_to_scan):
        payload = await http.get_json(HYPIXEL_V2_SKYBLOCK_AUCTIONS, params={"page": page})
        if not payload.get("success", False):
            break
        pages_scanned += 1
        stop = await scan(payload)
        if stop:
            break

    return best, prices, pages_scanned

# --------------------
# Networth (best-effort)
# --------------------
class PriceCache:
    def __init__(self):
        self._bazaar: Optional[Dict[str, Any]] = None
        self._bazaar_ts = 0.0
        self._bin: Dict[str, Tuple[int, float]] = {}

    async def bazaar(self, ttl_s: int = 120) -> Dict[str, Any]:
        now = time.time()
        if self._bazaar and (now - self._bazaar_ts) < ttl_s:
            return self._bazaar
        data = await http.get_json(HYPIXEL_BAZAAR, headers=hypixel_headers())
        if not data.get("success", False):
            raise RuntimeError(data.get("cause", "Bazaar error"))
        self._bazaar = data
        self._bazaar_ts = now
        return data

    def get_bin(self, name: str, ttl_s: int = 300) -> Optional[int]:
        hit = self._bin.get(name.lower())
        if not hit:
            return None
        price, ts = hit
        if (time.time() - ts) > ttl_s:
            return None
        return price

    def set_bin(self, name: str, price: int):
        self._bin[name.lower()] = (price, time.time())

price_cache = PriceCache()

async def mojang_uuid(username: str) -> str:
    data = await http.get_json(MOJANG_UUID_URL.format(username=username))
    undashed = data.get("id")
    if not undashed:
        raise ValueError("Username not found / Mojang API failed.")
    return undashed

def decode_inventory_items(data_b64: str) -> List[Any]:
    raw = base64.b64decode(data_b64)
    unzipped = gzip.decompress(raw)
    nbt = NBTFile(fileobj=io.BytesIO(unzipped))
    out = []
    if "i" in nbt:
        for comp in nbt["i"]:
            out.append(comp)
    return out

def extract_item(comp) -> Tuple[Optional[str], Optional[str], int]:
    try:
        count = int(comp.get("Count", 1).value)
    except Exception:
        count = 1

    sky_id = None
    disp = None
    try:
        tag = comp["tag"]
        if "ExtraAttributes" in tag and "id" in tag["ExtraAttributes"]:
            sky_id = str(tag["ExtraAttributes"]["id"].value)
        if "display" in tag and "Name" in tag["display"]:
            disp = parse_mc_json_text(str(tag["display"]["Name"].value))
    except Exception:
        pass

    return sky_id, disp, count

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

    baz = await price_cache.bazaar()
    products = (baz.get("products") or {})

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
                sky_id, disp, count = extract_item(comp)
                if not sky_id and not disp:
                    continue
                items.append((sky_id, disp, count))
        except Exception:
            continue

    valued = 0
    detail: List[Tuple[str, int, int, str]] = []
    skipped = defaultdict(int)

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

    # limited BIN pricing (avoid rate limits)
    unique: List[str] = []
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
            best, _, _ = await lowest_bin(name, max_pages=3, max_hits=250)
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
        "items_read": len(items),
    }
    return total, breakdown

# --------------------
# Hypixel status (HTML parse)
# --------------------
async def hypixel_network_status() -> Tuple[str, List[str]]:
    """
    Best-effort: fetch status.hypixel.net and extract top banner + a few components.
    """
    html = await http.get_text(HYPIXEL_STATUS_PAGE)
    # Title-like banner often includes "All Systems Operational"
    banner = "Unknown"
    m = re.search(r'All Systems Operational|Partial System Outage|Major Outage|Degraded Performance|Under Maintenance', html, re.I)
    if m:
        banner = m.group(0)

    # Grab a few component lines if present (very best-effort)
    components = []
    for name in ["Hypixel Minecraft Server", "SkyBlock", "Bed Wars", "SkyWars", "The Pit", "Housing"]:
        if re.search(re.escape(name) + r".{0,80}Operational", html, re.I | re.S):
            components.append(f"{name}: Operational")
    return banner, components[:6]

# --------------------
# DuckDuckGo Instant Answer Search
# --------------------
async def ddg_search(query: str) -> Tuple[str, List[Tuple[str, str]]]:
    """
    Returns (summary, related links). This is NOT full Google-style search results.
    """
    params = {"q": query, "format": "json", "no_redirect": "1", "no_html": "1"}
    data = await http.get_json(DDG_INSTANT, params=params)

    summary = ""
    if data.get("AbstractText"):
        summary = data["AbstractText"]
    elif data.get("Answer"):
        summary = str(data["Answer"])
    elif data.get("Heading"):
        summary = f"Results for: {data['Heading']}"
    else:
        summary = "No instant answer found. Try a more specific query."

    links: List[Tuple[str, str]] = []
    # RelatedTopics can be a list of dicts or nested "Topics"
    def collect_topics(arr):
        for t in arr:
            if "Topics" in t:
                collect_topics(t["Topics"])
            else:
                txt = t.get("Text")
                url = t.get("FirstURL")
                if txt and url and len(links) < 5:
                    links.append((txt, url))

    if isinstance(data.get("RelatedTopics"), list):
        collect_topics(data["RelatedTopics"])

    return summary, links

# --------------------
# Discord bot
# --------------------
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
    await http.start()
    try:
        await sync_commands()
    except Exception as e:
        print("Slash sync failed:", e)
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")

# --------------------
# Commands (PUBLIC responses)
# --------------------
@bot.tree.command(name="ping", description="Bot latency + uptime")
async def ping_cmd(interaction: discord.Interaction):
    uptime_s = int(time.time() - START_TIME)
    latency_ms = int(bot.latency * 1000)
    await interaction.response.send_message(
        f"🏓 Pong! **{latency_ms}ms** | Uptime: **{dt.timedelta(seconds=uptime_s)}**"
    )

@bot.tree.command(name="status", description="Hypixel network status")
async def status_cmd(interaction: discord.Interaction):
    await interaction.response.defer()  # public
    try:
        banner, components = await hypixel_network_status()
        msg = f"🟢 **Hypixel Status:** **{banner}**"
        if components:
            msg += "\n" + "\n".join(f"• {c}" for c in components)
        msg += "\n(From status.hypixel.net)"
        await interaction.followup.send(msg)
    except Exception as e:
        await interaction.followup.send(f"❌ Failed to fetch Hypixel status: `{e}`")

@bot.tree.command(name="calc", description="Safe calculator (supports sqrt/sin/cos/pi, etc.)")
@app_commands.describe(expression="Example: 2*(3+4) or sqrt(144) or sin(pi/2)")
async def calc_cmd(interaction: discord.Interaction, expression: str):
    try:
        result = safe_eval(expression)
        out = str(int(round(result))) if abs(result - round(result)) < 1e-12 else str(result)
        await interaction.response.send_message(f"🧮 `{expression}` = **{out}**")
    except Exception as e:
        await interaction.response.send_message(f"❌ Couldn’t compute that: `{e}`")

@bot.tree.command(name="timer", description="Set a timer. You will be notified in DMs.")
@app_commands.describe(seconds="1 to 86400", note="Optional note")
async def timer_cmd(interaction: discord.Interaction, seconds: int, note: Optional[str] = None):
    if seconds <= 0 or seconds > 86400:
        await interaction.response.send_message("❌ Timer must be between 1 and 86400 seconds.")
        return

    await interaction.response.send_message(f"⏳ Timer set for **{seconds}s**. I’ll DM you when it’s done.")
    user = interaction.user
    note_txt = f"\nNote: {note}" if note else ""

    async def fire():
        await asyncio.sleep(seconds)
        try:
            await user.send(f"⏰ Timer done! ({seconds}s){note_txt}")
        except discord.Forbidden:
            # User has DMs closed
            try:
                await interaction.followup.send(f"⚠️ I can’t DM you. Please enable DMs from server members.")
            except Exception:
                pass

    bot.loop.create_task(fire())

@bot.tree.command(name="search", description="Search (free best-effort) using DuckDuckGo Instant Answer")
@app_commands.describe(query="What you want to search")
async def search_cmd(interaction: discord.Interaction, query: str):
    await interaction.response.defer()
    try:
        summary, links = await ddg_search(query)
        embed = discord.Embed(title="🔎 Search", description=summary, timestamp=dt.datetime.utcnow())
        embed.add_field(name="Query", value=query, inline=False)
        if links:
            embed.add_field(
                name="Related",
                value="\n".join([f"• {txt}\n{url}" for txt, url in links]),
                inline=False,
            )
        embed.set_footer(text="Note: Instant Answer ≠ full Google results.")
        await interaction.followup.send(embed=embed)
    except Exception as e:
        await interaction.followup.send(f"❌ Search failed: `{e}`")

@bot.tree.command(name="auction", description="Lowest BIN + stats (v2 auctions)")
@app_commands.describe(item="Item name", pages="Pages to scan (1-10)")
async def auction_cmd(interaction: discord.Interaction, item: str, pages: int = 3):
    pages = max(1, min(pages, 10))
    await interaction.response.defer()

    try:
        best, prices, scanned = await lowest_bin(item, max_pages=pages, max_hits=400)
    except Exception as e:
        await interaction.followup.send(f"❌ Auction lookup failed: `{e}`")
        return

    if not prices:
        await interaction.followup.send(f"😕 No BIN auctions found for **{item}** (scanned {scanned} page(s)).")
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

    await interaction.followup.send(embed=embed)

@bot.tree.command(name="crystalpc", description="Crystal price check (BIN)")
@app_commands.describe(crystal="e.g. Jasper Crystal", pages="Pages to scan (1-10)")
async def crystalpc_cmd(interaction: discord.Interaction, crystal: str, pages: int = 5):
    pages = max(1, min(pages, 10))
    await interaction.response.defer()

    q = crystal.strip()
    if "crystal" not in q.lower():
        q = f"{q} crystal"

    try:
        best, prices, scanned = await lowest_bin(q, max_pages=pages, max_hits=450)
    except Exception as e:
        await interaction.followup.send(f"❌ Crystal check failed: `{e}`")
        return

    if not best or not prices:
        await interaction.followup.send(f"😕 No BIN auctions found for **{q}** (scanned {scanned} page(s)).")
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

    await interaction.followup.send(embed=embed)

@bot.tree.command(name="uuid", description="Get UUID for a Minecraft username")
@app_commands.describe(username="Minecraft username")
async def uuid_cmd(interaction: discord.Interaction, username: str):
    await interaction.response.defer()
    try:
        u = await mojang_uuid(username)
        await interaction.followup.send(f"🧾 `{username}` → `{u}`")
    except Exception as e:
        await interaction.followup.send(f"❌ UUID lookup failed: `{e}`")

@bot.tree.command(name="networth", description="Networth (best-effort): purse+bank+inventories (Bazaar + limited BIN)")
@app_commands.describe(username="Minecraft username")
async def networth_cmd(interaction: discord.Interaction, username: str):
    await interaction.response.defer()

    if not HYPIXEL_API_KEY:
        await interaction.followup.send("❌ HYPIXEL_API_KEY is missing in Railway Variables.")
        return

    try:
        uuid_undashed = await mojang_uuid(username)
    except Exception as e:
        await interaction.followup.send(f"❌ Couldn’t resolve UUID for `{username}`: `{e}`")
        return

    # Fetch profile list (v2)
    try:
        payload = await http.get_json(
            HYPIXEL_V2_SKYBLOCK_PROFILES,
            params={"uuid": uuid_undashed},
            headers=hypixel_headers(),
        )
        if not payload.get("success", False):
            raise RuntimeError(payload.get("cause", "Unknown profiles error"))
    except Exception as e:
        await interaction.followup.send(f"❌ Hypixel profiles lookup failed: `{e}`")
        return

    profile = pick_most_recent_profile(payload, uuid_undashed)
    if not profile:
        await interaction.followup.send(
            "😕 No SkyBlock profile found. (Or the player has SkyBlock API disabled / inventories disabled.)"
        )
        return

    try:
        total, b = await compute_networth(profile, uuid_undashed)
    except Exception as e:
        await interaction.followup.send(f"❌ Networth compute failed: `{e}`")
        return

    detail = b["detail"]
    top_lines = sorted(detail, key=lambda x: x[2], reverse=True)[:6]
    skipped_count = len(b["skipped"])

    embed = discord.Embed(
        title="🧾 Networth (best-effort)",
        description=(
            f"Player: **{username}**\n"
            f"Profile: **{b['profile_name']}**\n"
            f"Items read: **{b['items_read']}**\n\n"
            f"**Total:** {coins_fmt(total)}\n"
            f"Purse: {coins_fmt(b['purse'])}\n"
            f"Bank: {coins_fmt(b['bank'])}\n"
            f"Valued Items: {coins_fmt(b['valued_items'])}\n"
        ),
        timestamp=dt.datetime.utcnow(),
    )

    if top_lines:
        embed.add_field(
            name="Top valued items (sample)",
            value="\n".join([f"• {name} x{cnt}: {coins_fmt(val)} ({src})" for (name, cnt, val, src) in top_lines]),
            inline=False,
        )

    if skipped_count > 0:
        embed.add_field(
            name="Unpriced / skipped items",
            value=f"{skipped_count} unique item(s) couldn’t be priced this run (no bazaar id / no BIN / lookup limit).",
            inline=False,
        )

    embed.set_footer(text="Tip: Player must enable SkyBlock API 'Inventory' for full inventory networth.")
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="about", description="About this bot")
async def about_cmd(interaction: discord.Interaction):
    await interaction.response.send_message(
        "🤖 SkyBlock Utility Bot\n"
        "Commands: /status /networth /auction /crystalpc /calc /timer /search /uuid /ping\n"
        "Hosted on Railway."
    )

# --------------------
# Run
# --------------------
bot.run(DISCORD_TOKEN)
