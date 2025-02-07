import os
import re
import random
import logging
import time
import signal
from io import BytesIO
import json
import aiohttp
import asyncio
import requests
import psycopg2 
from psycopg2.pool import SimpleConnectionPool
import threading
import gc
from concurrent.futures import ThreadPoolExecutor
import random
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ParseMode, InputMediaPhoto, InputFile
from telegram.ext import Updater, CommandHandler, CallbackQueryHandler, CallbackContext, MessageHandler, Filters
from bip_utils import (
    Bip39MnemonicGenerator,
    Bip39SeedGenerator,
    Bip44,
    Bip44Coins,
    Bip44Changes,
    Bip39WordsNum,
)
from dotenv import load_dotenv
from datetime import datetime

# Load environment variables
load_dotenv()

TELEGRAM_BOT_TOKEN = "7755709244:AAGHs3uNznBfsOcXEbPxMh5sea2f2B_zQ7E"
DATABASE_URL = "postgresql://neondb_owner:npg_6lEVnoMdurO1@ep-icy-heart-a8munbh2-pooler.eastus2.azure.neon.tech/neondb?sslmode=require"

# Admin ID (update with your actual Telegram admin ID)
ADMIN_ID = 6268276296

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    handlers=[
        logging.FileHandler("bot.log"),  # Log file
        logging.StreamHandler(),        # Console output for Railway
    ],
)

# Initialize a connection pool
db_pool = SimpleConnectionPool(
    1, 10,  # Min and max connections
    DATABASE_URL
)



# Thread pool for managing concurrent scans
scan_executor = ThreadPoolExecutor(max_workers=10)

# Assuming COOLDOWN_TIME is 5 seconds (can be adjusted)
COOLDOWN_TIME = 5

# Dictionary to store the last command time for each user
user_last_command_time = {}

# Global variables for connection and cursor
db_connection = None
cursor = None

def get_db_connection_from_pool():
    try:
        if db_pool:
            conn = db_pool.getconn()
            if conn.closed:
                conn = psycopg2.connect(DATABASE_URL)
            return conn
        else:
            raise Exception("Database pool not initialized.")
    except Exception as e:
        logging.error(f"Error getting connection from pool: {e}")
        raise e

def release_db_connection(conn):
    try:
        if db_pool and conn:
            db_pool.putconn(conn)
    except Exception as e:
        logging.error(f"Error releasing connection: {e}")

# Database setup
def get_db_connection():
    """Ensure that the database connection is active."""
    global db_connection
    try:
        # Initialize or reconnect the database connection if closed
        if db_connection is None or db_connection.closed:
            db_connection = psycopg2.connect(DATABASE_URL)
            logging.info("Database connection established.")
        return db_connection
    except Exception as e:
        logging.error("Error connecting to the database: %s", str(e))
        raise e


def shutdown_handler(signum, frame):
    save_active_users()
    logging.info("Bot is shutting down. Active users saved.")

signal.signal(signal.SIGINT, shutdown_handler)
signal.signal(signal.SIGTERM, shutdown_handler)


def get_db_cursor(connection=None):
    """Ensure the cursor is created from an active connection."""
    global cursor
    if connection is None:
        connection = get_db_connection()

    try:
        # Create a new cursor if it doesn't exist or is closed
        if cursor is None or cursor.closed:
            cursor = connection.cursor()
            logging.info("Database cursor created.")
        return cursor
    except psycopg2.OperationalError as e:
        logging.error("Database cursor error: %s", str(e))
        connection = get_db_connection()  # Reconnect if connection lost
        cursor = connection.cursor()
        logging.info("Cursor reinitialized.")
        return cursor

# Example of using the connection and cursor
# Get connection and cursor
db_connection = get_db_connection()
cursor = get_db_cursor(db_connection)

def execute_query(query, params=None, retries=3):
    try:
        with db_pool.getconn() as conn:
            with conn.cursor() as cursor:
                cursor.execute(query, params or ())
                conn.commit()
    except psycopg2.Error as e:
        logging.error(f"Database error: {e}")
        if retries > 0:
            logging.info(f"Retrying query. Attempts left: {retries}")
            execute_query(query, params, retries - 1)
        else:
            raise


def create_tables():
    # Create masterkeys table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS masterkeys (
            id SERIAL PRIMARY KEY,
            key VARCHAR(255) NOT NULL,
            expiration TIMESTAMP,
            can_use_booster BOOLEAN DEFAULT FALSE
        )
    """)

    # Create seeds table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS seeds (
            id SERIAL PRIMARY KEY,
            seed VARCHAR(255) NOT NULL,
            balance NUMERIC NOT NULL,
            chance_rate NUMERIC NOT NULL,
            added_by BIGINT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Create admins table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS admins (
            id SERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL UNIQUE,
            username VARCHAR(255) NOT NULL
        )
    """)

    # Create user_keys table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_keys (
            user_id BIGINT PRIMARY KEY,
            key VARCHAR(255) NOT NULL UNIQUE
        )
    """)

    # Add the 'username' column if it doesn't exist
    cursor.execute("ALTER TABLE user_keys ADD COLUMN IF NOT EXISTS username VARCHAR(255)")

    # Populate default values for the 'username' column
    cursor.execute("UPDATE user_keys SET username = 'Unknown' WHERE username IS NULL")

    # Enforce the NOT NULL constraint on the 'username' column
    cursor.execute("ALTER TABLE user_keys ALTER COLUMN username SET NOT NULL")

    # Commit the changes to the database
    db_connection.commit()

def save_active_users():
    with open("active_chat_ids.json", "w") as f:
        json.dump(list(active_chat_ids), f)
    logging.info("Active user list saved.")

def load_active_users():
    global active_chat_ids
    try:
        with open("active_chat_ids.json", "r") as f:
            active_chat_ids = set(json.load(f))
        logging.info(f"Loaded {len(active_chat_ids)} active users.")
    except FileNotFoundError:
        active_chat_ids = set()
        logging.info("No active users file found. Starting fresh.")


# Initialize logger
logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# Notification message
NOTIFICATION_MESSAGE = (
    "🔄 **Bot Update Notification** 🔄\n\n"
    "✨ The bot has been updated with new features and fixes!\n"
    "💡 Use /start to explore the latest updates and ensure you're ready to scan wallets.\n\n"
    "Thank you for using Wallet Scanner Bot! 🚀"
)


#keywordfiner

# Set to track active users (chat IDs) during this session
active_chat_ids = set()

# Function to track users
def track_user(update: Update, context: CallbackContext) -> None:
    """Track active user chat IDs."""
    chat_id = update.message.chat.id
    active_chat_ids.add(chat_id)
    logger.info(f"Tracking user: {chat_id}")

# Notify all users (synchronous version)
def notify_all_users(context: CallbackContext) -> None:
    """Broadcast the update notification to all active users."""
    app = context.bot  # Access the bot instance from context
    if not active_chat_ids:
        logger.info("No active users to notify.")
        return

    logger.info(f"Notifying {len(active_chat_ids)} active users about the update.")
    for chat_id in active_chat_ids:
        try:
            app.send_message(chat_id=chat_id, text=NOTIFICATION_MESSAGE)  # Synchronous call
            logger.info(f"Notified chat ID: {chat_id}")
        except Exception as e:
            logger.error(f"Failed to notify chat {chat_id}: {e}")

def clear_logs(update: Update, context: CallbackContext) -> None:
    user_id = update.message.chat.id

    # Ensure only the admin can clear logs
    if user_id != ADMIN_ID:
        update.message.reply_text("❌ You don't have permission to clear the logs.")
        return

    log_file = "bot.log"  # Path to the log file

    try:
        with open(log_file, "w") as file:
            file.write("")  # Clear the file contents
        update.message.reply_text("✅ All logs have been cleared.")
    except Exception as e:
        logging.error("Error clearing logs: %s", str(e))
        update.message.reply_text("❌ An error occurred while clearing the logs.")

# Call the functions to create the tables
create_tables()

# Global variables
user_scan_status = {}
user_keys = {}

def bip():
    return Bip39MnemonicGenerator().FromWordsNumber(Bip39WordsNum.WORDS_NUM_12)

def bip44_wallet_from_seed(seed, coin_type):
    seed_bytes = Bip39SeedGenerator(seed).Generate()
    bip44_mst_ctx = Bip44.FromSeed(seed_bytes, coin_type)
    bip44_acc_ctx = (
        bip44_mst_ctx.Purpose()
        .Coin()
        .Account(0)
        .Change(Bip44Changes.CHAIN_EXT)
        .AddressIndex(0)
    )
    address = bip44_acc_ctx.PublicKey().ToAddress()
    return address

def check_balance(address, blockchain='eth', retries=3):
    # Updated API URLs (keys are appended dynamically for MATIC and SOL)
    API_URLS = {
        'ETH': 'https://api.etherscan.io/api',
        'BNB': 'https://api.bscscan.com/api',
        'MATIC': 'https://polygon-mainnet.g.alchemy.com/v2/',  # Updated URL for Alchemy API
        'BTC': 'https://api.blockcypher.com/v1/btc/main/addrs',
        'SOL': 'https://solana-mainnet.g.alchemy.com/v2/',  # Updated URL for Solana Alchemy API
        'TRX': 'https://api.trongrid.io/v1/accounts',
    }

    # Updated API keys
    API_KEYS = {
        'ETH': ['FQP5IPEJ8AX6CPK36KA4SA83JM8Q8GE536', 'QJ1KK3WKKXPJY3YS1J7D92X28VHW3IZ3WS', 'XXCIS9AM5MTK3SYX6KUQJR78WS1RVV2JJ5', 'CBPTJ93NUMZWX9GZCDFTMGRUS9IC7EH3BQ', 'WXWU1HKNC5VTA3R2C2GSXSFA9X28G1I7M2', 'GURBM457ARBWUZB3S2H4GUJ1VJW81QYD4H', '6KGNW5GJGW75XBZAG4ZJ1MFTK485SCSGDX'],
        'BNB': ['65M94C8PQJ7D2XV2I1HRAGPAUBS4M6SEBM', 'WBRXW5TIW8695GJ9MYI4GMQ697E9IXTME9', 'T5TJ95BRV5C39EHGEGUE2C66CCWVT2AEWH', 'DR65PS97WNCUC8TNTVNBWM8II8KXSMYYNS'],
        'MATIC': ['zoMCKvF33iDsnOOypDHFM7Kz7DcXYGf6'],  # Alchemy key for Polygon
        'BTC': ['caf89b72dce148db9ec9ab91b7752535'],  # Blockcypher key for BTC
        'SOL': ['zoMCKvF33iDsnOOypDHFM7Kz7DcXYGf6'],  # Alchemy key for Solana
        'TRX': ['36fccbf8-4fb6-4359-9da1-9eb4731112dd', '9622305c-560a-4cbd-8f64-37b4cf17b24b', '938868d6-021f-4450-91a3-a2d282564e60', '59518681-695e-4a73-aacf-254bd39ebd84'],
    }

    # Ensure blockchain is uppercase for dictionary matching
    blockchain = blockchain.upper()
    
    # Get API URL and keys for the given blockchain
    url = API_URLS.get(blockchain)
    api_keys = API_KEYS.get(blockchain)

    if not url or not api_keys:
        logging.error(f"Unsupported blockchain or missing API keys: {blockchain}")
        return 0

    # Rotate API keys in case of limits (simple rotation logic)
    for attempt in range(retries):
        for api_key_to_use in api_keys:
            try:
                logging.info(f"Checking balance for {blockchain} on attempt {attempt + 1} using API key: {api_key_to_use}")

                # Construct request based on blockchain
                if blockchain == 'ETH':
                    full_url = f"{url}?module=account&action=balance&address={address}&tag=latest&apikey={api_key_to_use}"
                    response = requests.get(full_url, timeout=10)
                    response.raise_for_status()
                    data = response.json()
                    balance = int(data['result']) / 1e18  # Convert Wei to ETH
                    return balance

                elif blockchain == 'BNB':
                    full_url = f"{url}?module=account&action=balance&address={address}&tag=latest&apikey={api_key_to_use}"
                    response = requests.get(full_url, timeout=10)
                    response.raise_for_status()
                    data = response.json()
                    balance = int(data['result']) / 1e18  # Convert Wei to BNB
                    return balance

                elif blockchain == 'MATIC':
                    full_url = f"{url}{api_key_to_use}"
                    payload = {
                        "jsonrpc": "2.0",
                        "method": "eth_getBalance",
                        "params": [address, "latest"],
                        "id": 1
                    }
                    response = requests.post(full_url, json=payload, timeout=10)  # Use POST method
                    response.raise_for_status()
                    data = response.json()
                    balance = int(data['result'], 16) / 1e18  # Convert from Wei to MATIC
                    return balance

                elif blockchain == 'BTC':
                    full_url = f"{url}/{address}/balance?token={api_key_to_use}"
                    response = requests.get(full_url, timeout=10)
                    response.raise_for_status()
                    data = response.json()
                    balance = int(data['balance']) / 1e8  # Convert Satoshi to BTC
                    return balance

                elif blockchain == 'SOL':
                    full_url = url + api_key_to_use
                    payload = {
                        "jsonrpc": "2.0",
                        "method": "getBalance",
                        "params": [address],
                        "id": 1
                    }
                    response = requests.post(full_url, json=payload, timeout=10)
                    response.raise_for_status()
                    data = response.json()
                    balance = data.get('result', {}).get('value', 0) / 1e9  # Convert Lamports to SOL
                    return balance

                elif blockchain == 'TRX':
                    full_url = f"{url}/{address}?apikey={api_key_to_use}"
                    response = requests.get(full_url, timeout=10)
                    response.raise_for_status()
                    data = response.json()
                    balance = data['data'][0]['balance'] / 1e6  # Convert SUN to TRX
                    return balance

                else:
                    logging.error(f"Unsupported blockchain: {blockchain}")
                    return 0

            except requests.exceptions.RequestException as e:
                logging.error(f"HTTP error for {blockchain} (address: {address}): {e}")
                time.sleep(1)  # Backoff before retrying
            except ValueError as e:
                logging.error(f"Error parsing response for {blockchain} (address: {address}): {e}")
                break  # Do not retry for parsing errors

    logging.error(f"Failed to retrieve balance for {blockchain} (address: {address}) after {retries} attempts")
    return 0

        
def bip44_btc_seed_to_address(seed):
    # Generate the seed from the mnemonic
    seed_bytes = Bip39SeedGenerator(seed).Generate()
    
    # Derive the Bitcoin address using the BIP44 standard
    bip44_mst_ctx = Bip44.FromSeed(seed_bytes, Bip44Coins.BITCOIN)
    bip44_acc_ctx = bip44_mst_ctx.Purpose().Coin().Account(0)
    bip44_chg_ctx = bip44_acc_ctx.Change(Bip44Changes.CHAIN_EXT)
    bip44_addr_ctx = bip44_chg_ctx.AddressIndex(0)
    
    # Generate the BTC address
    btc_address = bip44_addr_ctx.PublicKey().ToAddress()
    return btc_address

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

def start(update: Update, context: CallbackContext) -> None:
    user_id = update.message.chat.id
    username = update.message.chat.username or "Unknown"

    # Track the user
    if user_id not in active_chat_ids:
        active_chat_ids.add(user_id)
        logging.info(f"User added to active_chat_ids: {user_id} (@{username})")

    # Implement cooldown to prevent spamming
    current_time = time.time()
    last_command_time = user_last_command_time.get(user_id, 0)

    if current_time - last_command_time < COOLDOWN_TIME:
        remaining_time = int(COOLDOWN_TIME - (current_time - last_command_time))
        update.message.reply_text(
            f"⏳ Please wait **{remaining_time} seconds** before using this command again. Thank you for your patience! 🙏"
        )
        return

    # Update the last command time
    user_last_command_time[user_id] = current_time

    # Ensure the connection and cursor are active before executing the query
    cursor = get_db_cursor()

    try:
        # Check if the user has already redeemed a key
        cursor.execute("SELECT key FROM user_keys WHERE user_id = %s", (user_id,))
        user_data = cursor.fetchone()

        # Friendly Welcome Message
        if user_data:
            key = user_data[0]
            update.message.reply_text(
                f"🎉 **Welcome back, @{username}!** 🎉\n\n"
                f"🔑 **Key Redeemed:** `{key}`\n"
                "✨ You're all set to start scanning wallets! 🚀\n\n"
                "You can also use the Account Checker feature to process account files. Click the button below to access it! 😎"
            )
        else:
            update.message.reply_text(
                "🌟 **Welcome to Wallet Scanner Bot!** 🌟\n\n"
                "👋 Hi there! To begin, you’ll need to redeem a key.\n"
                "🔑 Use `/redeem <key>` to unlock the scanning features.\n\n"
                "Once redeemed, you'll gain access to the Account Checker and other features! 💰"
            )

        # Send a friendly welcoming photo
        update.message.reply_photo(
            photo="https://i.ibb.co/vkWdrtj/photo-6251221056064964503-c.jpg",
            caption=(
                "✨ **Welcome Aboard!** We’re thrilled to have you here. Let’s get started! 🚀\n\n"
                "💵 Key Prices:\n\n"
                "1 Day key : 15$ | 0.07 SOL\n"
                "1 Week key : 70$ | 0.34 SOL\n"
                "1 Month key : 300$ | 1.46 SOL\n\n"
                "If you want to buy a key, just send a message to ADMIN: @emran080"
            )
        )

        # Display the main menu with options
        keyboard = [
            [InlineKeyboardButton("ℹ️ About the Bot", callback_data='about')],
            [InlineKeyboardButton("🪙 Blockchain Options", callback_data='blockchain_options')],
            [InlineKeyboardButton("🚀 Start Scan (Booster Mode)", callback_data='start_scan_booster')],
            [InlineKeyboardButton("⛔ Stop Scan", callback_data='stop_scan')],
            [InlineKeyboardButton("🔑 Show Keys", callback_data='show_keys')], # New button
        ]

        reply_markup = InlineKeyboardMarkup(keyboard)
        update.message.reply_text(
            "👇 **What would you like to do next?**\n\n"
            "Choose an option below to get started with Wallet Scanner Bot! 🔥",
            reply_markup=reply_markup
        )

    except Exception as e:
        logging.error(f"Error in start command: {e}")
        update.message.reply_text(
            "❌ **Oops! Something went wrong.**\nPlease try again later or contact support if the issue persists."
        )


def blockchain_options(update: Update, context: CallbackContext) -> None:
    """
    Display blockchain options for the user, aligned with the updated API base URLs.
    """
    current_time = time.time()
    user_id = None

    # Check if the request is from a message or callback query
    if update.message:
        user_id = update.message.chat.id

        # Cooldown check for messages
        last_command_time = user_last_command_time.get(user_id, 0)
        if current_time - last_command_time < COOLDOWN_TIME:
            remaining_time = int(COOLDOWN_TIME - (current_time - last_command_time))
            update.message.reply_text(f"⏳ Please wait {remaining_time} seconds before using this option again.")
            return

        # Update last command time for the user
        user_last_command_time[user_id] = current_time

        # Send blockchain options as a new message
        blockchain_keyboard = [
            [InlineKeyboardButton("🪙 Ethereum (ETH)", callback_data='start_scan_eth')],
            [InlineKeyboardButton("🪙 Binance Smart Chain (BNB)", callback_data='start_scan_bnb')],
            [InlineKeyboardButton("🪙 Polygon (MATIC)", callback_data='start_scan_matic')],
            [InlineKeyboardButton("🪙 Solana (SOL)", callback_data='start_scan_sol')],
            [InlineKeyboardButton("🪙 Bitcoin (BTC)", callback_data='start_scan_btc')],
            [InlineKeyboardButton("🪙 Tron (TRX)", callback_data='start_scan_trx')],
            [InlineKeyboardButton("⬅️ Back", callback_data='back_to_main')],
        ]
        reply_markup = InlineKeyboardMarkup(blockchain_keyboard)

        update.message.reply_text(
            text="🌐 **Select a Blockchain** 🌐\n\nChoose a blockchain to start scanning:",
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN
        )

    elif update.callback_query:
        query = update.callback_query
        user_id = query.message.chat.id

        # Cooldown check for callback queries
        last_command_time = user_last_command_time.get(user_id, 0)
        if current_time - last_command_time < COOLDOWN_TIME:
            remaining_time = int(COOLDOWN_TIME - (current_time - last_command_time))
            query.answer(
                f"⏳ Please wait {remaining_time} seconds before using this option again.",
                show_alert=True
            )
            return

        # Update last command time for the user
        user_last_command_time[user_id] = current_time

        blockchain_keyboard = [
            [InlineKeyboardButton("🪙 Ethereum (ETH)", callback_data='start_scan_eth')],
            [InlineKeyboardButton("🪙 Binance Smart Chain (BNB)", callback_data='start_scan_bnb')],
            [InlineKeyboardButton("🪙 Polygon (MATIC)", callback_data='start_scan_matic')],
            [InlineKeyboardButton("🪙 Solana (SOL)", callback_data='start_scan_sol')],
            [InlineKeyboardButton("🪙 Bitcoin (BTC)", callback_data='start_scan_btc')],
            [InlineKeyboardButton("🪙 Tron (TRX)", callback_data='start_scan_trx')],
            [InlineKeyboardButton("⬅️ Back", callback_data='back_to_main')],
        ]
        reply_markup = InlineKeyboardMarkup(blockchain_keyboard)

        # Answer the callback query and edit the message
        query.answer()
        query.edit_message_text(
            text="🌐 **Select a Blockchain** 🌐\n\nChoose a blockchain to start scanning:",
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN
        )

def back_to_main(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    if query:
        try:
            query.answer()  # Acknowledge the callback
        except Exception as e:
            logging.error(f"Error answering callback query: {e}")

    # Edit the message to display the main menu again
    try:
        query.edit_message_text(
            text="👇 **What would you like to do next?**\n\n"
                 "Choose an option below to get started with Wallet Scanner Bot! 🔥",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("ℹ️ About the Bot", callback_data='about')],
                [InlineKeyboardButton("🪙 Blockchain Options", callback_data='blockchain_options')],
                [InlineKeyboardButton("🚀 Start Scan (Booster Mode)", callback_data='start_scan_booster')],
                [InlineKeyboardButton("⛔ Stop Scan", callback_data='stop_scan')],
                [InlineKeyboardButton("🔑 Show Keys", callback_data='show_keys')],
            ])
        )
    except Exception as e:
        logging.error(f"Error editing callback query message: {e}")

# Show admin list command
def show_admin(update: Update, context: CallbackContext) -> None:
    user_id = update.message.chat.id
    if user_id != ADMIN_ID:
        update.message.reply_text("❌ You don't have permission to view the admin list.")
        return

    try:
        cursor.execute("SELECT username, user_id FROM admins")
        admins = cursor.fetchall()

        if admins:
            admin_list = "\n".join([f"💻 @{admin[0]} [{admin[1]}]" for admin in admins])
            update.message.reply_text(f"👥 **Admin list** 👥\n\n{admin_list}", parse_mode="Markdown")
        else:
            update.message.reply_text("❌ No admins found.")
    except Exception as e:
        logging.error("Error fetching admin list: %s", str(e))
        update.message.reply_text("❌ An error occurred while fetching the admin list.")

def is_admin(user_id):
    try:
        # First, check if the user_id is the hardcoded admin ID
        if user_id == ADMIN_ID:
            return True  # User is the hardcoded admin

        # Otherwise, check in the database
        conn = get_db_connection()  # Ensure you get a valid database connection
        with conn.cursor() as cursor:  # Use `with` to ensure the cursor is properly closed
            cursor.execute("SELECT user_id FROM admins WHERE user_id = %s", (user_id,))
            return cursor.fetchone() is not None
    except Exception as e:
        logging.error(f"Error checking admin status: {e}")
        return False


def start_scan_by_id(user_id, blockchain, message, booster):
    """
    Initiates a wallet scan for the specified blockchain or all blockchains (booster mode).
    """
    # Starting scan message
    message.reply_text(
        f"✨ Awesome! Starting a scan on {blockchain.upper()}... 🌍\n"
        f"🌱 Seed: .......\n🏦 Address: .......\n🔄 Scanned wallets: 0"
    )

    # Initialize user scan status
    user_scan_status[user_id] = {'is_scanning': False}

    # Handle scanning for booster mode or a single blockchain
    if booster and blockchain == 'all':
        blockchains = ['eth', 'bnb', 'matic', 'btc', 'sol', 'trx']
        for chain in blockchains:
            threading.Thread(target=scan_wallets, args=(user_id, chain, message, True)).start()
    else:
        threading.Thread(target=scan_wallets, args=(user_id, blockchain, message, False)).start()

    # Confirmation message
    message.reply_text(
        f"🚀 Your {blockchain.upper()} scan has started! Sit tight while we search for treasure 🤑!"
    )

def stop_all_scans(update: Update, context: CallbackContext) -> None:
    user_id = update.message.chat.id
    # Only the admin can stop all scans
    if user_id != ADMIN_ID:
        update.message.reply_text("❌ You don't have permission to stop all scans.")
        return
    
    # Stop all scans
    for user in user_scan_status:
        user_scan_status[user]['is_scanning'] = False
    
    update.message.reply_text("🛑 All scans have been stopped by the admin. bot updated or fixed bug")

def stop_scan(update: Update, context: CallbackContext) -> None:
    user_id = update.callback_query.message.chat.id
    if user_id not in user_scan_status or not user_scan_status[user_id].get('is_scanning', False):
        update.callback_query.message.reply_text("⛔ No active scan to stop.")
        return

    user_scan_status[user_id]['is_scanning'] = False
    update.callback_query.message.reply_text("🛑 Scanning stopped.")

def scan_wallets(user_id, blockchain, message, booster=False):
    try:
        # Ensure cursor is initialized properly
        cursor = db_connection.cursor()  # Initialize the cursor here

        # Fetch previous scan state from the database
        cursor.execute("""
            SELECT wallets_scanned 
            FROM scan_logs 
            WHERE user_id = %s AND blockchain = %s
        """, (user_id, blockchain))
        result = cursor.fetchone()
        previous_scanned_count = result[0] if result else 0

        # Initialize scanning status
        user_scan_status[user_id] = {
            'is_scanning': True,
            'wallets_scanned': previous_scanned_count
        }

        # Check booster permission from the masterkeys table
        cursor.execute("""
            SELECT can_use_booster 
            FROM masterkeys 
            WHERE key = (
                SELECT key FROM user_keys WHERE user_id = %s
            )
        """, (user_id,))
        booster_data = cursor.fetchone()
        booster_allowed = booster_data[0] if booster_data else False

        if booster and not booster_allowed:
            booster = False
            message.reply_text("⚠️ You don't have permission to use booster mode. Continuing scan without booster.")

        # Determine blockchain type
        blockchain_map = {
            'eth': Bip44Coins.ETHEREUM,
            'bnb': Bip44Coins.BINANCE_SMART_CHAIN,
            'matic': Bip44Coins.POLYGON,
            'btc': Bip44Coins.BITCOIN,
            'sol': Bip44Coins.SOLANA,
            'trx': Bip44Coins.TRON  # Updated for TRON
        }

        coin_type = blockchain_map.get(blockchain)
        if not coin_type:
            message.reply_text("❌ Unsupported blockchain selected.")
            return

        # Start watchdog thread
        watchdog_thread = threading.Thread(target=watchdog, args=(user_id, blockchain, message, booster))
        watchdog_thread.daemon = True
        watchdog_thread.start()

        # Begin scanning
        while user_scan_status[user_id]['is_scanning']:
            seed = bip()
            if blockchain == 'btc':
                address = bip44_btc_seed_to_address(seed)
            else:
                address = bip44_wallet_from_seed(seed, coin_type)

            balance = check_balance(address, blockchain)
            user_scan_status[user_id]['wallets_scanned'] += 1

            # Update database with scan progress
            cursor.execute("""
                INSERT INTO scan_logs (user_id, blockchain, wallets_scanned)
                VALUES (%s, %s, %s)
                ON CONFLICT (user_id, blockchain)
                DO UPDATE SET wallets_scanned = %s
            """, (user_id, blockchain, user_scan_status[user_id]['wallets_scanned'],
                  user_scan_status[user_id]['wallets_scanned']))
            db_connection.commit()

            # Update status message every 50 scans
            if user_scan_status[user_id]['wallets_scanned'] % 50 == 0:
                try:
                    message.edit_text(
                        f"```\n"
                        f"✨ Scanning {blockchain.upper()}...\n"
                        f"🌱 Seed: {seed}\n"
                        f"🏦 Address: {address}\n"
                        f"🔄 Wallets scanned: {user_scan_status[user_id]['wallets_scanned']}\n"
                        f"⏳ Working hard to find balances! 🌟\n"
                        f"```",
                        parse_mode=ParseMode.MARKDOWN
                    )
                except Exception as e:
                    logging.error(f"Error editing message: {e}")

            # If balance found, notify user and stop scanning
            if balance > 0:
                message.reply_text(
                    f"🎉 Found a wallet with balance!\n"
                    f"🌱 Seed: {seed}\n"
                    f"🏦 Address: {address}\n"
                    f"💰 Balance: {balance} {blockchain.upper()}"
                )
                user_scan_status[user_id]['is_scanning'] = False
                break

            time.sleep(0.5 if booster else 0.9)

    except Exception as e:
        logging.error(f"Error in scan_wallets: {e}")
        message.reply_text("❌ An error occurred during the scan.")
    finally:
        # Clean up scanning status
        user_scan_status[user_id]['is_scanning'] = False

# Remove admin command
def remove_admin(update: Update, context: CallbackContext) -> None:
    user_id = update.message.chat.id
    if user_id != ADMIN_ID:
        update.message.reply_text("❌ You don't have permission to remove admins.")
        return

    args = context.args
    if len(args) < 1:
        update.message.reply_text("❌ Usage: /remove_admin <id>")
        return

    admin_id = int(args[0])

    try:
        execute_query("DELETE FROM admins WHERE user_id = %s", (admin_id,))
        update.message.reply_text(f"✅ Admin removed: [{admin_id}]")
    except Exception as e:
        logging.error("Error removing admin: %s", str(e))
        update.message.reply_text("❌ An error occurred while removing the admin.")

def derive_address(seed, blockchain):
    try:
        # Map blockchain to corresponding Bip44Coins or custom derivation methods
        blockchain_map = {
            'btc': Bip44Coins.BITCOIN,
            'eth': Bip44Coins.ETHEREUM,
            'bnb': Bip44Coins.BINANCE_SMART_CHAIN,
            'matic': Bip44Coins.POLYGON,
            'trx': 'TRX',  # Custom handling for TRON
            'sol': 'SOL'   # Custom handling for Solana
        }

        # Check if the blockchain is supported
        coin_type = blockchain_map.get(blockchain)
        if not coin_type:
            raise ValueError(f"Unsupported blockchain: {blockchain}")

        # Derive address based on blockchain type
        if blockchain == 'btc':
            return bip44_btc_seed_to_address(seed)  # Custom BTC derivation logic
        elif blockchain in ['trx']:
            return tron_seed_to_address(seed)  # Custom TRON derivation logic
        elif blockchain in ['sol']:
            return solana_seed_to_address(seed)  # Custom Solana derivation logic
        else:
            # General EVM-compatible chains (ETH, BNB, MATIC)
            return bip44_wallet_from_seed(seed, coin_type)

    except Exception as e:
        raise ValueError(f"Error deriving address for blockchain={blockchain}: {e}")

def start_scan(update: Update, context: CallbackContext) -> None:
    try:
        user_id = update.callback_query.message.chat.id

        # Check if the user has a valid key in the database
        cursor.execute("SELECT key FROM user_keys WHERE user_id = %s", (user_id,))
        user_data = cursor.fetchone()

        if not user_data:
            update.callback_query.message.reply_text(
                "❌ Oops! You need a valid key to start scanning. Please redeem one first!"
            )
            return

        # Map callback data to blockchain names
        blockchain_map = {
            'start_scan_eth': 'eth',
            'start_scan_bnb': 'bnb',
            'start_scan_matic': 'matic',
            'start_scan_btc': 'btc',
            'start_scan_sol': 'sol',
            'start_scan_pol': 'pol',
            'start_scan_booster': 'all',  # Booster scans all blockchains
            'start_scan_trx': 'trx',  # Added TRON support
        }

        # Get blockchain to scan from callback data
        blockchain = blockchain_map.get(update.callback_query.data, '')
        if not blockchain:
            update.callback_query.message.reply_text("❌ Invalid blockchain selection.")
            return

        # Check if a scan is already running
        if user_scan_status.get(user_id, {}).get('is_scanning', False):
            update.callback_query.message.reply_text(
                "🔍 Hey there! A scan is already running. Please stop the current scan before starting a new one."
            )
            return

        # Update scan status
        user_scan_status[user_id] = {'is_scanning': True}

        # Start scanning message
        message = update.callback_query.message.reply_text(
            f"✨ Awesome! Starting a scan on {blockchain.upper()}... 🌍\n"
            f"🌱 Seed: .......\n🏦 Address: .......\n🔄 Scanned wallets: 0"
        )

        # Handle scanning for booster mode or a single blockchain
        if blockchain == 'all':  # Booster mode scans all blockchains
            chains = ['eth', 'bnb', 'matic', 'btc', 'sol', 'pol', 'trx']  # Removed AVAX
            for chain in chains:
                try:
                    scan_executor.submit(scan_wallets, user_id, chain, message, True)
                except Exception as e:
                    logging.error(f"Error starting scan for blockchain {chain}: {e}")
                    message.reply_text(f"❌ Failed to start scan for {chain.upper()}. Please try again later.")
        else:
            try:
                scan_executor.submit(scan_wallets, user_id, blockchain, message, False)
            except Exception as e:
                logging.error(f"Error starting scan for blockchain {blockchain}: {e}")
                message.reply_text(f"❌ Failed to start scan for {blockchain.upper()}. Please try again later.")

        # Send confirmation message
        update.callback_query.message.reply_text(
            f"🚀 Your {blockchain.upper()} scan has started! Sit tight while we search for treasure 🤑!"
        )

    except Exception as e:
        logging.error(f"Error in start_scan: {e}")
        update.callback_query.message.reply_text("❌ An error occurred while starting the scan. Please try again.")


def watchdog(user_id, blockchain, context, booster=False):
    while user_scan_status[user_id]['is_scanning']:
        prev_scanned = user_scan_status[user_id]['wallets_scanned']
        time.sleep(120)  # Check every 2 minutes
        
        # Check if no wallets have been scanned during this period
        if user_scan_status[user_id]['wallets_scanned'] == prev_scanned:
            # Restart the scan if no progress is detected
            user_scan_status[user_id]['is_scanning'] = False
            context.bot.send_message(chat_id=user_id, text=f"⚠️ The scan on {blockchain.upper()} seems to have paused. Restarting now...")
            start_scan_by_id(user_id, blockchain, context.bot, booster)  # Restart scan

def add_admin(update: Update, context: CallbackContext) -> None:
    user_id = update.message.chat.id
    if user_id != ADMIN_ID:
        update.message.reply_text("❌ You don't have permission to add admins.")
        return

    args = context.args
    if len(args) < 2:
        update.message.reply_text("❌ Usage: /add_admin <id> <username>")
        return

    new_admin_id = int(args[0])
    username = args[1]

    try:
        # Add the new admin to the database
        cursor.execute(
            "INSERT INTO admins (user_id, username) VALUES (%s, %s) ON CONFLICT (user_id) DO NOTHING",
            (new_admin_id, username),
        )
        db_connection.commit()

        if cursor.rowcount > 0:
            update.message.reply_text(f"✅ Admin added: {username} [{new_admin_id}]")
        else:
            update.message.reply_text(f"ℹ️ Admin [{new_admin_id}] already exists.")
    except Exception as e:
        logging.error("Error adding admin: %s", str(e))
        update.message.reply_text("❌ An error occurred while adding the admin.")

def create_key(update: Update, context: CallbackContext) -> None:
    user_id = update.message.chat.id
    
    # Check if the user is an admin
    if not is_admin(user_id):
        update.message.reply_text("❌ You don't have permission to create keys.")
        return

    args = context.args
    if len(args) < 3:
        update.message.reply_text("❌ Usage: /create_key <key> <expiration (DD-MM-YYYY)> <booster (true/false)>")
        return

    key = args[0]
    expiration_str = args[1]
    booster = args[2].lower()

    try:
        expiration = datetime.strptime(expiration_str, "%d-%m-%Y")
    except ValueError:
        update.message.reply_text("❌ Invalid expiration date format. Please use DD-MM-YYYY.")
        return

    if booster not in ['true', 'false']:
        update.message.reply_text("❌ Booster must be either 'true' or 'false'.")
        return

    booster_mode = booster == 'true'

    try:
        cursor.execute("INSERT INTO masterkeys (key, expiration, can_use_booster) VALUES (%s, %s, %s)",
                       (key, expiration, booster_mode))
        db_connection.commit()
        update.message.reply_text(f"✅ Key created: {key}\n📅 Expiration: {expiration_str}\n🚀 Booster mode: {booster_mode}")
    except Exception as e:
        logging.error("Error creating key: %s", str(e))
        update.message.reply_text("❌ An error occurred while creating the key.")

def button_callback(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    if query.data == 'about':
        about_callback(update, context)
    elif query.data in ['start_scan_eth', 'start_scan_bnb', 'start_scan_matic', 'start_scan_trx', 'start_scan_btc', 'start_scan_pol', 'start_scan_booster']:
        start_scan(update, context)
    elif query.data == 'stop_scan':
        stop_scan(update, context)
    elif query.data == 'show_keys':
        show_keys(update, context)

def about_callback(update: Update, context: CallbackContext) -> None:
    update.callback_query.message.reply_text(
        f"```\n"
        f"✨ Welcome to the Wallet Scanner Bot! ✨\n\n"
        f"🔍 This bot is your ultimate tool for finding wallets with balances across the following networks:\n"
        f"  - 🌐 Ethereum (ETH)\n"
        f"  - 🔶 Binance Smart Chain (BSC)\n"
        f"  - 🟣 Polygon (MATIC)\n"
        f"  - 🪙 Bitcoin (BTC)\n"
        f"  - 🌞 Solana (SOL)\n"
        f"  - 🚀 Tron (TRX)\n\n"
        f"💡 Features:\n"
        f"  - 🔑 Redeem keys to unlock powerful scanning capabilities.\n"
        f"  - 🚀 Use Booster Mode for faster, simultaneous scanning across all supported networks.\n\n"
        f"📖 How to Get Started:\n"
        f"  1️⃣ Use /redeem <key> to activate your scanning access.\n"
        f"  2️⃣ Select the blockchain network you want to scan.\n"
        f"  3️⃣ Sit back and let the bot do the work for you!\n\n"
        f"```"
        "💬 Need help or have questions? Send massage to @emran080 to learn more about the bot's features.\n\n"
        "Happy scanning! 🤑"
    )
    
def redeem(update: Update, context: CallbackContext) -> None:
    user_id = update.message.chat.id
    username = update.message.chat.username or "Unknown"
    args = context.args

    if len(args) < 1:
        update.message.reply_text("🔑 Please provide a key to redeem: /redeem <key>")
        return

    new_key = args[0]

    try:
        # Check if the key exists in the masterkeys table
        cursor.execute("SELECT key, can_use_booster FROM masterkeys WHERE key = %s", (new_key,))
        key_data = cursor.fetchone()

        if not key_data:
            update.message.reply_text("❌ Invalid key. Please try again.")
            return

        # Check if the key is already redeemed
        cursor.execute("SELECT user_id FROM user_keys WHERE key = %s", (new_key,))
        existing_user = cursor.fetchone()

        if existing_user and existing_user[0] != user_id:
            update.message.reply_text("❌ This key is already redeemed by another user.")
            return

        # Insert or update the user's record
        cursor.execute(
            """
            INSERT INTO user_keys (user_id, key, username)
            VALUES (%s, %s, %s)
            ON CONFLICT (user_id)
            DO UPDATE SET key = %s, username = %s
            """,
            (user_id, new_key, username, new_key, username)
        )
        db_connection.commit()

        # Notify the user of successful redemption
        booster_enabled = key_data[1]  # Assuming 'can_use_booster' is at index 1
        message = (
            f"✅ Key redeemed successfully!\n"
            f"🔑 Key: {new_key}\n"
            f"🚀 Booster mode: {'Enabled' if booster_enabled else 'Disabled'}\n"
            f"🎉 Welcome, @{username}!"
        )
        update.message.reply_text(message)
    except Exception as e:
        logging.error(f"Error during key redemption: {e}")
        update.message.reply_text("❌ An error occurred while redeeming the key. Please try again later.")

def optimize_memory():
    while True:
        gc.collect()  # Force garbage collection to clear unused memory
        time.sleep(600)  # Perform memory cleanup every 10 minutes

def remove_key(update: Update, context: CallbackContext) -> None:
    user_id = update.message.chat.id

    # Check if the user is an admin
    if not is_admin(user_id):
        update.message.reply_text("❌ You don't have permission to remove keys.")
        return

    args = context.args
    if len(args) < 1:
        update.message.reply_text("❌ Usage: /remove_key <key>")
        return

    key = args[0]

    try:
        # Remove the key from the masterkeys table
        cursor.execute("DELETE FROM masterkeys WHERE key = %s", (key,))
        masterkey_deleted = cursor.rowcount > 0

        # Remove the key from the user_keys table
        cursor.execute("DELETE FROM user_keys WHERE key = %s", (key,))
        userkey_deleted = cursor.rowcount > 0

        db_connection.commit()

        # Feedback to the admin
        if masterkey_deleted or userkey_deleted:
            update.message.reply_text(f"✅ Key removed successfully: {key}")
        else:
            update.message.reply_text("❌ Key not found in either masterkeys or user_keys table.")
    except Exception as e:
        logging.error("Error removing key: %s", str(e))
        update.message.reply_text("❌ An error occurred while removing the key.")

def show_keys(update: Update, context: CallbackContext) -> None:
    user_id = update.callback_query.message.chat.id

    # Ensure the user is an admin
    if not is_admin(user_id):
        update.callback_query.message.reply_text("❌ You don't have permission to view the keys.")
        return

    conn = None
    try:
        # Get the database connection and cursor
        conn = get_db_connection()
        cursor = conn.cursor()

        # Current time for expiration check
        current_time = datetime.now()

        # Update expiration status
        cursor.execute("""
            UPDATE masterkeys
            SET is_expired = CASE 
                                WHEN expiration < %s THEN TRUE 
                                ELSE FALSE 
                            END
        """, (current_time,))
        conn.commit()

        # Delete expired keys (optional)
        cursor.execute("DELETE FROM masterkeys WHERE expiration < %s", (current_time,))
        conn.commit()

        # Fetch the keys with expiration status
        cursor.execute("""
            SELECT u.user_id, u.username, u.key, m.expiration, m.is_expired, m.can_use_booster
            FROM user_keys u
            JOIN masterkeys m ON u.key = m.key
        """)
        user_keys = cursor.fetchall()

        if user_keys:
            keys_list = "\n\n".join([
                f"👤 User: @{row[1]} ({row[0]})\n"
                f"🔑 Key: {row[2]}\n"
                f"📅 Expiration: {row[3]}\n"
                f"❗ Expired: {'Yes' if row[4] else 'No'}\n"
                f"🚀 Booster Mode: {'Enabled' if row[5] else 'Disabled'}"
                for row in user_keys
            ])
            update.callback_query.message.reply_text(f"🗝️ Current Keys:\n\n{keys_list}")
        else:
            update.callback_query.message.reply_text("❌ No keys have been redeemed.")
    except Exception as e:
        logging.error(f"Error fetching keys: {e}")
        update.callback_query.message.reply_text("❌ An error occurred while fetching the keys.")
    finally:
        # Close the database connection
        if conn:
            conn.close()

def admin_panel(update: Update, context: CallbackContext) -> None:
    user_id = update.message.chat.id if update.message else update.callback_query.message.chat.id

    # Check admin permissions
    if user_id != ADMIN_ID:
        update.message.reply_text("❌ You don't have permission to access the admin panel.") if update.message else \
            update.callback_query.answer("❌ You don't have permission.", show_alert=True)
        return

    # Define keyboard options for the admin panel
    keyboard = [
        [InlineKeyboardButton("➕ Create Key", callback_data='admin_create_key')],
        [InlineKeyboardButton("➖ Remove Key", callback_data='admin_remove_key')],
        [InlineKeyboardButton("🔑 Show Keys", callback_data='admin_show_keys')],
        [InlineKeyboardButton("🛑 Stop All Scans", callback_data='admin_stop_all_scans')],
        [InlineKeyboardButton("🌱 Add Seed", callback_data='admin_add_seed')],
        [InlineKeyboardButton("📜 Show Seeds", callback_data='admin_show_seed')],
        [InlineKeyboardButton("⬅️ Back to Main Menu", callback_data='back_to_main')]
    ]

    # Generate the reply markup
    reply_markup = InlineKeyboardMarkup(keyboard)

    # Admin panel message text
    message_text = (
        "🔐 **Admin Panel** 🔐\n\n"
        "Welcome, Admin! Choose an action from the options below:\n\n"
        "🗂️ Manage keys and seeds efficiently.\n"
        "🚦 Control scanning operations.\n"
        "🔧 Customize app functionalities.\n\n"
        "💡 *Note*: Actions are for administrators only."
    )

    # Send the admin panel menu
    if update.message:
        update.message.reply_text(
            text=message_text,
            reply_markup=reply_markup,
            parse_mode="Markdown"
        )
    elif update.callback_query:
        update.callback_query.message.edit_text(
            text=message_text,
            reply_markup=reply_markup,
            parse_mode="Markdown"
        )


# Dictionary to track ongoing checks per user
ongoing_checks = {}



# Add seed command
def add_seed(update: Update, context: CallbackContext) -> None:
    user_id = update.message.chat.id

    # Ensure the user is an admin
    if user_id != ADMIN_ID:
        update.message.reply_text("\u274C You don't have permission to add seeds.")
        return

    args = context.args
    if len(args) < 3:
        update.message.reply_text("\u274C Usage: /add_seed <12_words> <balance> <chance rate (1-100%)>")
        return

    seed = args[0]
    balance = float(args[1])
    chance_rate = float(args[2])

    if not (1 <= chance_rate <= 100):
        update.message.reply_text("\u274C Chance rate must be between 1% and 100%.")
        return

    try:
        cursor.execute(
            """
            INSERT INTO seeds (seed, balance, chance_rate, added_by)
            VALUES (%s, %s, %s, %s)
            """,
            (seed, balance, chance_rate, user_id),
        )
        db_connection.commit()
        update.message.reply_text(f"\u2705 Seed added successfully!\n\ud83c\udf31 Seed: {seed}\n\ud83d\udcb5 Balance: {balance}\n\u26a1 Chance Rate: {chance_rate}%")
    except Exception as e:
        update.message.reply_text("\u274C Failed to add seed.")
        logging.error(f"Error adding seed: {e}")

def show_seed(update: Update, context: CallbackContext) -> None:
    user_id = update.message.chat.id

    # Ensure the user is an admin
    if user_id != ADMIN_ID:
        update.message.reply_text("❌ You don't have permission to view seeds.")
        return

    try:
        # Re-establish database connection if needed
        if db_connection is None or db_connection.closed:
            logging.warning("Database connection was closed. Attempting to reconnect.")
            db_connection = get_db_connection()  # Assuming you have a `get_db_connection` function
            cursor = db_connection.cursor()

        # Execute the query to fetch seeds
        cursor.execute("SELECT id, seed, balance, chance_rate FROM seeds")
        seeds = cursor.fetchall()

        if seeds:
            seed_list = []
            for seed in seeds:
                seed_list.append(
                    f"📌 **ID**: {seed[0]}\n"
                    f"🌱 **Seed**: `{seed[1]}`\n"
                    f"💰 **Balance**: {seed[2]}\n"
                    f"⚡ **Chance Rate**: {seed[3]}%\n"
                )

            # Combine seeds into chunks to avoid exceeding message limits
            seed_chunks = [
                "\n".join(seed_list[i:i + 10]) for i in range(0, len(seed_list), 10)
            ]

            # Send each chunk as a separate message
            for chunk in seed_chunks:
                update.message.reply_text(
                    f"🔑 **Seeds List**:\n\n{chunk}", parse_mode=ParseMode.MARKDOWN
                )
        else:
            update.message.reply_text("❌ No seeds found in the database.")

    except psycopg2.Error as db_error:
        update.message.reply_text("❌ Failed to fetch seeds due to a database error.")
        logging.error(f"Database error fetching seeds: {db_error}")
    except Exception as e:
        update.message.reply_text("❌ Failed to fetch seeds due to an unknown error.")
        logging.error(f"Error showing seeds: {e}")


        
def send_seed(update: Update, context: CallbackContext) -> None:
    user_id = update.message.chat.id

    # Ensure the user is an admin
    if user_id != ADMIN_ID:
        update.message.reply_text("❌ You don't have permission to send seeds.")
        return

    args = context.args
    if len(args) < 5:
        update.message.reply_text("❌ Usage: /send_seed <seed_id> <user_id> <address> <balance> <blockchain>")
        return

    try:
        # Parse and validate arguments
        seed_id = int(args[0])
        target_user_id = int(args[1])
        address = args[2]
        balance = float(args[3])
        blockchain = args[4].lower()

        # Validate the blockchain
        valid_blockchains = ['eth', 'bnb', 'matic', 'avax', 'btc', 'sol', 'pol']
        if blockchain not in valid_blockchains:
            update.message.reply_text(f"❌ Unsupported blockchain: {blockchain.upper()}. Supported: {', '.join(valid_blockchains).upper()}")
            return

        # Fetch the seed details
        cursor.execute("SELECT seed FROM seeds WHERE id = %s", (seed_id,))
        seed = cursor.fetchone()

        if not seed:
            update.message.reply_text("❌ Seed not found. Please check the seed ID.")
            return

        # Update the seed details in the database
        cursor.execute("""
            UPDATE seeds 
            SET address = %s, balance = %s, blockchain = %s
            WHERE id = %s
        """, (address, balance, blockchain, seed_id))
        db_connection.commit()

        # Prepare the message for the recipient
        message = (
            f"🎉 **Found a wallet with balance!**\n\n"
            f"🌱 **Seed:** `{seed[0]}`\n"
            f"🏦 **Address:** `{address}`\n"
            f"💰 **Balance:** {balance} {blockchain.upper()}\n\n"
            f"🔗 *Use this wallet responsibly!*"
        )

        # Send the message to the target user
        context.bot.send_message(target_user_id, message, parse_mode=ParseMode.MARKDOWN)

        # Confirm to the admin
        update.message.reply_text(f"✅ Seed {seed_id} sent successfully to user {target_user_id}.")

    except ValueError as e:
        update.message.reply_text("❌ Invalid input. Please check the arguments and try again.")
        logging.error(f"Input validation error: {e}")
    except Exception as e:
        update.message.reply_text("❌ Failed to send the seed. Please check the logs for details.")
        logging.error(f"Error sending seed: {e}", exc_info=True)

def handle_admin_callback(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    user_id = query.message.chat.id

    # Ensure the user is an admin
    if user_id != ADMIN_ID:
        query.answer("\u274C Unauthorized action.", show_alert=True)
        return

    query.answer()

    # Handle the callback data
    if query.data == 'admin_create_key':
        query.edit_message_text("\u2795 Use /create_key <key> <expiration (DD-MM-YYYY)> <booster (true/false)> to create a key.")
    elif query.data == 'admin_remove_key':
        query.edit_message_text("\u2796 Use /remove_key <key> to remove a key.")
    elif query.data == 'admin_show_keys':
        show_keys(update, context)  # Call the existing show_keys function
    elif query.data == 'admin_stop_all_scans':
        stop_all_scans(update, context)  # Call the existing stop_all_scans function
    elif query.data == 'admin_add_seed':
        query.edit_message_text("\u2795 Use /add_seed <12_words> <balance> <chance rate (1-100%)> to add a seed.")
    elif query.data == 'admin_show_seed':
        show_seed(update, context)  # Call the existing show_seed function






def pod_command(update: Update, context: CallbackContext) -> None:
    user_id = update.message.chat.id

    # Check if the user is an admin
    if not is_admin(user_id):
        update.message.reply_text("❌ You don't have permission to use this command.")
        return

    # Clear any previous broadcast state
    context.user_data.pop('waiting_for_broadcast', None)

    # Ask for input
    update.message.reply_text("📝 Please send the message or upload a photo with a caption for broadcasting.")
    context.user_data['waiting_for_broadcast'] = True

def handle_broadcast_input(update: Update, context: CallbackContext) -> None:
    if not context.user_data.get('waiting_for_broadcast', False):
        return

    # Check if it's a text message or a photo
    if update.message.text:
        message = update.message.text
        send_broadcast(message=message, photo=None, context=context)
    elif update.message.photo:
        photo = update.message.photo[-1].file_id  # Get the highest resolution photo
        caption = update.message.caption or ""
        send_broadcast(message=caption, photo=photo, context=context)

    # Clear the waiting state
    context.user_data['waiting_for_broadcast'] = False
    update.message.reply_text("✅ Broadcast sent successfully!")

def send_broadcast(message: str, photo: str, context: CallbackContext) -> None:
    bot = context.bot
    failed_count = 0

    # Notify all users in active_chat_ids
    for chat_id in active_chat_ids:
        try:
            if photo:
                bot.send_photo(chat_id=chat_id, photo=photo, caption=message)
            else:
                bot.send_message(chat_id=chat_id, text=message)
        except Exception as e:
            logging.error(f"Failed to send broadcast to {chat_id}: {e}")
            failed_count += 1

    logging.info(f"Broadcast complete. Failed to notify {failed_count} users.")




def main() -> None:
    # Start memory optimization in a separate thread
    memory_thread = threading.Thread(target=optimize_memory)
    memory_thread.daemon = True  # Ensure it runs in the background
    memory_thread.start()

    # Initialize bot and dispatcher
    updater = Updater(TELEGRAM_BOT_TOKEN)
    dispatcher = updater.dispatcher

    # Command handlers
    dispatcher.add_handler(CommandHandler("start", start))
    dispatcher.add_handler(CommandHandler("redeem", redeem))
    dispatcher.add_handler(CommandHandler("create_key", create_key))
    dispatcher.add_handler(CommandHandler("remove_key", remove_key))
    dispatcher.add_handler(CommandHandler("clear_logs", clear_logs))
    dispatcher.add_handler(CommandHandler("admin_panel", admin_panel))
    dispatcher.add_handler(CommandHandler("send_seed", send_seed)) 
    dispatcher.add_handler(CallbackQueryHandler(handle_admin_callback, pattern='admin_.*'))
    dispatcher.add_handler(CommandHandler("pod", pod_command))
    dispatcher.add_handler(MessageHandler(Filters.text | Filters.photo, handle_broadcast_input))
    dispatcher.add_handler(CallbackQueryHandler(back_to_main, pattern='back_to_main'))
    dispatcher.add_handler(CallbackQueryHandler(about_callback, pattern='about'))
    dispatcher.add_handler(CommandHandler("stop_allscans", stop_all_scans))
    dispatcher.add_handler(CommandHandler("add_admin", add_admin))
    dispatcher.add_handler(CommandHandler("remove_admin", remove_admin))
    dispatcher.add_handler(CommandHandler("show_admin", show_admin))
    # Callback query handler
    dispatcher.add_handler(CallbackQueryHandler(button_callback))

    # Start the bot
    updater.start_polling()

       # Notify users when the bot starts
    updater.job_queue.run_once(notify_all_users, 0)


    logger = logging.getLogger(__name__)
    updater.idle()

if __name__ == '__main__':
    main()
