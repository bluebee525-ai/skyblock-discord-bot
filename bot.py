import os
import discord
from discord.ext import commands, tasks
import requests
import aiosqlite
import time

# ---------------- ENVIRONMENT VARIABLES ----------------
TOKEN = os.getenv("DISCORD_TOKEN")
LTC_ADDRESS = os.getenv("LTC_ADDRESS")
SOL_ADDRESS = os.getenv("SOL_ADDRESS")
LOG_CHANNEL = int(os.getenv("LOG_CHANNEL", 0))
GUILD_ID = int(os.getenv("GUILD_ID", 0))  # server ID for instant slash commands

if not TOKEN or not LTC_ADDRESS or not SOL_ADDRESS or LOG_CHANNEL == 0 or GUILD_ID == 0:
    raise ValueError("Please set DISCORD_TOKEN, LTC_ADDRESS, SOL_ADDRESS, LOG_CHANNEL, and GUILD_ID in environment variables!")

# ---------------- INTENTS ----------------
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

# ---------------- DATABASE ----------------
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
        await db.execute("""
        CREATE TABLE IF NOT EXISTS notifications(
            id INTEGER PRIMARY KEY,
            user_id INTEGER,
            wallet TEXT,
            coin TEXT,
            last_tx TEXT
        )
        """)
        await db.commit()

# ---------------- PRICE ----------------
def get_price(symbol):
    url = f"https://api.coingecko.com/api/v3/simple/price?ids={symbol}&vs_currencies=usd"
    r = requests.get(url).json()
    return r[symbol]["usd"]

# ---------------- LTC / SOL TRANSACTIONS ----------------
def ltc_transactions():
    url = f"https://api.blockcypher.com/v1/ltc/main/addrs/{LTC_ADDRESS}"
    r = requests.get(url).json()
    return r.get("txrefs", [])

def sol_transactions():
    url = f"https://public-api.solscan.io/account/transactions?account={SOL_ADDRESS}"
    r = requests.get(url).json()
    return r

# ---------------- /INVOICE ----------------
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

# ---------------- /STATUS ----------------
@bot.tree.command(name="status")
async def status(interaction: discord.Interaction, invoice_id: int):
    async with aiosqlite.connect("invoices.db") as db:
        cursor = await db.execute("SELECT paid FROM invoices WHERE id=?", (invoice_id,))
        row = await cursor.fetchone()

    if not row:
        await interaction.response.send_message("Invoice not found")
        return

    await interaction.response.send_message("Paid ✅" if row[0] else "Waiting for payment ⏳")

# ---------------- /NOTIFY ----------------
@bot.tree.command(name="notify")
async def notify(interaction: discord.Interaction, wallet: str, coin: str):
    coin = coin.upper()
    if coin not in ["LTC","SOL"]:
        await interaction.response.send_message("Coin must be LTC or SOL")
        return

    async with aiosqlite.connect("invoices.db") as db:
        await db.execute(
            "INSERT INTO notifications(user_id, wallet, coin, last_tx) VALUES(?,?,?,?)",
            (interaction.user.id, wallet, coin, "")
        )
        await db.commit()

    await interaction.response.send_message(f"You will now be notified for transactions on {wallet} ({coin}) ✅")

# ---------------- PAYMENT WATCHER ----------------
@tasks.loop(seconds=30)
async def watcher():
    # Invoice check
    ltc = ltc_transactions()
    sol = sol_transactions()

    async with aiosqlite.connect("invoices.db") as db:
        # Invoice payments
        cursor = await db.execute("SELECT id,user_id,coin,amount,created FROM invoices WHERE paid=0")
        invoices = await cursor.fetchall()

        for inv in invoices:
            invoice_id, user_id, coin, amount, created = inv
            if time.time() - created > 1800:  # skip expired
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

        # Notify subscriptions
        cursor = await db.execute("SELECT id,user_id,wallet,coin,last_tx FROM notifications")
        notify_subs = await cursor.fetchall()

        for sub in notify_subs:
            sub_id, user_id, wallet, coin, last_tx = sub
            user = await bot.fetch_user(user_id)
            new_txid = None

            if coin == "LTC":
                url = f"https://api.blockcypher.com/v1/ltc/main/addrs/{wallet}"
                r = requests.get(url).json()
                txs = r.get("txrefs", [])
                for tx in txs:
                    txid = tx["tx_hash"]
                    if txid != last_tx:
                        new_txid = txid
                        break

            elif coin == "SOL":
                url = f"https://public-api.solscan.io/account/transactions?account={wallet}"
                r = requests.get(url).json()
                if len(r) > 0:
                    txid = r[0]["txHash"]
                    if txid != last_tx:
                        new_txid = txid

            if new_txid:
                await user.send(f"New {coin} transaction detected for wallet {wallet} ✅\nTXID: {new_txid}")
                await db.execute("UPDATE notifications SET last_tx=? WHERE id=?", (new_txid, sub_id))

        await db.commit()

# ---------------- READY ----------------
@bot.event
async def on_ready():
    print("Bot online ✅")
    await init_db()
    guild = discord.Object(id=GUILD_ID)
    await bot.tree.sync(guild=guild)
    print(f"Slash commands synced to guild {GUILD_ID} ✅")
    watcher.start()

bot.run(TOKEN)
