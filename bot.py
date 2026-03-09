import os
import discord
from discord.ext import commands, tasks
import requests
import aiosqlite
import time

# ---------- ENVIRONMENT VARIABLES ----------
TOKEN = os.getenv("DISCORD_TOKEN")
LTC_ADDRESS = os.getenv("LTC_ADDRESS")
SOL_ADDRESS = os.getenv("SOL_ADDRESS")
LOG_CHANNEL = int(os.getenv("LOG_CHANNEL", 0))  # default to 0 if not set

if not TOKEN or not LTC_ADDRESS or not SOL_ADDRESS or LOG_CHANNEL == 0:
    raise ValueError("Please set DISCORD_TOKEN, LTC_ADDRESS, SOL_ADDRESS, and LOG_CHANNEL in environment variables!")

# ---------- INTENTS ----------
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

# ---------- DATABASE ----------
async def init_db():
    async with aiosqlite.connect("invoices.db") as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS invoices(
            id INTEGER PRIMARY KEY,
            user_id INTEGER,
            coin TEXT,
            amount REAL,
            paid INTEGER,
            created INTEGER
        )
        """)
        await db.commit()

# ---------- PRICE ----------
def get_price(symbol):
    url = f"https://api.coingecko.com/api/v3/simple/price?ids={symbol}&vs_currencies=usd"
    r = requests.get(url).json()
    return r[symbol]["usd"]

# ---------- LTC CHECK ----------
def ltc_transactions():
    url = f"https://api.blockcypher.com/v1/ltc/main/addrs/{LTC_ADDRESS}"
    r = requests.get(url).json()
    return r.get("txrefs", [])

# ---------- SOL CHECK ----------
def sol_transactions():
    url = f"https://public-api.solscan.io/account/transactions?account={SOL_ADDRESS}"
    r = requests.get(url).json()
    return r

# ---------- INVOICE COMMAND ----------
@bot.tree.command(name="invoice")
async def invoice(interaction: discord.Interaction, usd: float, coin: str):
    coin = coin.upper()
    if coin not in ["LTC","SOL"]:
        await interaction.response.send_message("Coin must be LTC or SOL")
        return

    price = get_price("litecoin" if coin == "LTC" else "solana")
    crypto_amount = usd / price

    async with aiosqlite.connect("invoices.db") as db:
        cursor = await db.execute(
            "INSERT INTO invoices(user_id,coin,amount,paid,created) VALUES(?,?,?,?,?)",
            (interaction.user.id, coin, crypto_amount, 0, int(time.time()))
        )
        invoice_id = cursor.lastrowid
        await db.commit()

    embed = discord.Embed(
        title=f"Invoice #{invoice_id}",
        description=f"Send **{crypto_amount:.6f} {coin}** (~${usd})",
        color=0x00ff90
    )
    embed.add_field(name="LTC Address", value=LTC_ADDRESS, inline=False)
    embed.add_field(name="SOL Address", value=SOL_ADDRESS, inline=False)
    embed.set_footer(text="Expires in 30 minutes")
    await interaction.response.send_message(embed=embed)

# ---------- STATUS ----------
@bot.tree.command(name="status")
async def status(interaction: discord.Interaction, invoice_id: int):
    async with aiosqlite.connect("invoices.db") as db:
        cursor = await db.execute("SELECT paid FROM invoices WHERE id=?", (invoice_id,))
        row = await cursor.fetchone()

    if not row:
        await interaction.response.send_message("Invoice not found")
        return

    await interaction.response.send_message("Paid ✅" if row[0] else "Waiting for payment ⏳")

# ---------- PAYMENT WATCHER ----------
@tasks.loop(seconds=30)
async def watcher():
    ltc = ltc_transactions()
    sol = sol_transactions()

    async with aiosqlite.connect("invoices.db") as db:
        cursor = await db.execute(
            "SELECT id,user_id,coin,amount,created FROM invoices WHERE paid=0"
        )
        invoices = await cursor.fetchall()

        for inv in invoices:
            invoice_id, user_id, coin, amount, created = inv

            # Skip expired invoices
            if time.time() - created > 1800:
                continue

            if coin == "LTC":
                for tx in ltc:
                    if tx["confirmations"] < 2:
                        continue
                    tx_amount = tx["value"] / 100000000
                    if tx_amount >= amount:
                        txid = tx["tx_hash"]
                        await db.execute("UPDATE invoices SET paid=1 WHERE id=?", (invoice_id,))
                        user = await bot.fetch_user(user_id)
                        await user.send(f"Payment confirmed ✅\nInvoice #{invoice_id}\nTXID: {txid}")
                        log = bot.get_channel(LOG_CHANNEL)
                        if log:
                            await log.send(f"Invoice {invoice_id} paid\nUser: {user}\nTXID: {txid}")
                        break

            if coin == "SOL":
                for tx in sol:
                    txid = tx["txHash"]
                    await db.execute("UPDATE invoices SET paid=1 WHERE id=?", (invoice_id,))
                    user = await bot.fetch_user(user_id)
                    await user.send(f"SOL Payment confirmed ✅\nInvoice #{invoice_id}\nTXID: {txid}")
                    log = bot.get_channel(LOG_CHANNEL)
                    if log:
                        await log.send(f"Invoice {invoice_id} paid\nUser: {user}\nTXID: {txid}")
                    break

        await db.commit()

# ---------- READY ----------
@bot.event
async def on_ready():
    print("Bot online ✅")
    await init_db()
    await bot.tree.sync()
    watcher.start()

bot.run(TOKEN)
