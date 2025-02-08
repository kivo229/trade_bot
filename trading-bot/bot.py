import os
import logging
from datetime import datetime, timedelta
import pytz
import json
import sqlite3
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    JobQueue
)
import pandas as pd
from bs4 import BeautifulSoup
import paypalrestsdk
from dotenv import load_dotenv
import aiosqlite
import asyncio
from pathlib import Path

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# PayPal Configuration
paypalrestsdk.configure({
    "mode": os.getenv('PAYPAL_MODE', 'sandbox'),
    "client_id": os.getenv('PAYPAL_CLIENT_ID'),
    "client_secret": os.getenv('PAYPAL_SECRET')
})

# Database setup
async def setup_database():
    async with aiosqlite.connect('subscribers.db') as db:
        await db.execute('''
            CREATE TABLE IF NOT EXISTS subscribers (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                subscription_start TIMESTAMP,
                subscription_end TIMESTAMP,
                payment_id TEXT,
                is_active BOOLEAN
            )
        ''')
        await db.commit()

# Trading analysis functions (modified from your original code)
async def analyze_html_files(directory_path: str = "."):
    """Analyze all HTML files in the specified directory."""
    html_files = list(Path(directory_path).glob("*.html"))
    all_trades = []
    
    for html_file in html_files:
        try:
            with open(html_file, 'r', encoding='utf-8') as file:
                soup = BeautifulSoup(file.read(), 'html.parser')
                
            messages = soup.find_all('div', class_='message')
            for message in messages:
                text_element = message.find('div', class_='text')
                if text_element and "📊 Trading Report" in text_element.text:
                    trades = parse_trading_report(text_element.text, html_file.name)
                    all_trades.extend(trades)
        except Exception as e:
            logger.error(f"Error processing {html_file}: {e}")
    
    if all_trades:
        df = pd.DataFrame(all_trades)
        return analyze_trades(df)
    return None

def parse_trading_report(content: str, source_file: str):
    """Parse individual trading report content."""
    trades = []
    lines = content.split('\n')
    trade_sequence = 1
    
    for line in lines:
        if '->' in line and ';' in line:
            try:
                pair, time, action_result = line.split(';')
                action, result = action_result.split('->')
                
                hour = int(time.split(':')[0])
                session = 'MORNING' if 7 <= hour < 12 else 'AFTERNOON' if 15 <= hour < 20 else 'OTHER'
                
                trades.append({
                    'session': session,
                    'sequence_number': trade_sequence,
                    'pair': pair.strip(),
                    'time': time.strip(),
                    'result': 'WIN' if 'GAIN' in result else 'LOSS',
                    'hour': hour,
                    'source_file': source_file
                })
                trade_sequence += 1
            except Exception as e:
                logger.error(f"Error parsing trade line: {line}, {e}")
    
    return trades

def analyze_trades(df: pd.DataFrame):
    """Generate analysis report from trade data."""
    total_trades = len(df)
    overall_win_rate = (df['result'] == 'WIN').mean() * 100
    
    session_stats = df.groupby('session').agg({
        'result': ['count', lambda x: (x == 'WIN').mean() * 100]
    }).round(2)
    
    hour_stats = df.groupby('hour').agg({
        'result': ['count', lambda x: (x == 'WIN').mean() * 100]
    }).round(2)
    
    pair_stats = df.groupby('pair').agg({
        'result': ['count', lambda x: (x == 'WIN').mean() * 100]
    }).round(2)
    
    return {
        'total_trades': total_trades,
        'overall_win_rate': overall_win_rate,
        'session_stats': session_stats,
        'hour_stats': hour_stats,
        'pair_stats': pair_stats
    }

# Payment handling
async def create_payment(user_id: int, amount: float = 7.00):
    """Create PayPal payment for subscription."""
    payment = paypalrestsdk.Payment({
        "intent": "sale",
        "payer": {"payment_method": "paypal"},
        "redirect_urls": {
            "return_url": f"https://your-domain.com/success?user_id={user_id}",
            "cancel_url": "https://your-domain.com/cancel"
        },
        "transactions": [{
            "amount": {
                "total": str(amount),
                "currency": "USD"
            },
            "description": "Trading Bot Monthly Subscription"
        }]
    })
    
    if payment.create():
        return payment
    return None

# Subscription management
async def add_subscriber(user_id: int, username: str, payment_id: str):
    """Add new subscriber to database."""
    now = datetime.now()
    end_date = now + timedelta(days=30)
    
    async with aiosqlite.connect('subscribers.db') as db:
        await db.execute('''
            INSERT OR REPLACE INTO subscribers 
            (user_id, username, subscription_start, subscription_end, payment_id, is_active)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (user_id, username, now, end_date, payment_id, True))
        await db.commit()

async def check_subscription(user_id: int):
    """Check if user has active subscription."""
    async with aiosqlite.connect('subscribers.db') as db:
        async with db.execute(
            'SELECT is_active, subscription_end FROM subscribers WHERE user_id = ?',
            (user_id,)
        ) as cursor:
            result = await cursor.fetchone()
            if result:
                is_active, end_date = result
                return is_active and datetime.strptime(end_date, '%Y-%m-%d %H:%M:%S.%f') > datetime.now()
    return False

# Bot command handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command."""
    welcome_text = (
        "🤖 Welcome to the Trading Analysis Bot!\n\n"
        "Get detailed analysis of trading performance and automated updates.\n\n"
        "Available commands:\n"
        "/subscribe - Subscribe to analysis updates\n"
        "/status - Check subscription status\n"
        "/analysis - Get latest analysis\n"
        "/help - Show this help message"
    )
    await update.message.reply_text(welcome_text)

async def subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /subscribe command."""
    user_id = update.effective_user.id
    
    if await check_subscription(user_id):
        await update.message.reply_text("You already have an active subscription!")
        return
    
    payment = await create_payment(user_id)
    if payment:
        for link in payment.links:
            if link.method == "REDIRECT":
                keyboard = [[InlineKeyboardButton("Pay with PayPal", url=link.href)]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await update.message.reply_text(
                    "Click below to complete your subscription payment:",
                    reply_markup=reply_markup
                )
                return
    
    await update.message.reply_text("Sorry, there was an error creating the payment. Please try again later.")

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /status command."""
    user_id = update.effective_user.id
    
    async with aiosqlite.connect('subscribers.db') as db:
        async with db.execute(
            'SELECT subscription_end, is_active FROM subscribers WHERE user_id = ?',
            (user_id,)
        ) as cursor:
            result = await cursor.fetchone()
            
            if result:
                end_date, is_active = result
                end_date = datetime.strptime(end_date, '%Y-%m-%d %H:%M:%S.%f')
                days_left = (end_date - datetime.now()).days
                
                status_text = (
                    f"Subscription Status: {'Active' if is_active else 'Inactive'}\n"
                    f"Days Remaining: {days_left}\n"
                    f"Expiration Date: {end_date.strftime('%Y-%m-%d')}"
                )
            else:
                status_text = "You don't have an active subscription."
    
    await update.message.reply_text(status_text)

async def analysis(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /analysis command."""
    user_id = update.effective_user.id
    
    if not await check_subscription(user_id):
        await update.message.reply_text(
            "You need an active subscription to access analysis.\n"
            "Use /subscribe to get started!"
        )
        return
    
    await update.message.reply_text("Analyzing trading data... Please wait.")
    
    analysis_results = await analyze_html_files()
    if analysis_results:
        report = format_analysis_report(analysis_results)
        await update.message.reply_text(report)
    else:
        await update.message.reply_text("No trading data found to analyze.")

def format_analysis_report(results: dict) -> str:
    """Format analysis results into readable message."""
    report = [
        "📊 Trading Analysis Report\n",
        f"Total Trades: {results['total_trades']}",
        f"Overall Win Rate: {results['overall_win_rate']:.1f}%\n",
        "Session Performance:",
    ]
    
    for session, stats in results['session_stats'].iterrows():
        report.append(f"- {session}: {stats['result']['<lambda>']:.1f}% ({stats['result']['count']} trades)")
    
    report.append("\nBest Trading Hours:")
    top_hours = results['hour_stats'].sort_values(('result', '<lambda>'), ascending=False)[:3]
    for hour, stats in top_hours.iterrows():
        report.append(f"- {hour:02d}:00: {stats['result']['<lambda>']:.1f}% ({stats['result']['count']} trades)")
    
    return "\n".join(report)

async def send_periodic_updates(context: ContextTypes.DEFAULT_TYPE):
    """Send periodic analysis updates to subscribers."""
    analysis_results = await analyze_html_files()
    if not analysis_results:
        return
    
    report = format_analysis_report(analysis_results)
    
    async with aiosqlite.connect('subscribers.db') as db:
        async with db.execute('SELECT user_id FROM subscribers WHERE is_active = 1') as cursor:
            async for (user_id,) in cursor:
                try:
                    await context.bot.send_message(chat_id=user_id, text=report)
                except Exception as e:
                    logger.error(f"Error sending update to user {user_id}: {e}")

async def main():
    """Initialize and run the bot."""
    # Setup database
    await setup_database()
    
    # Initialize bot
    application = Application.builder().token(os.getenv('TELEGRAM_BOT_TOKEN')).build()
    
    # Add command handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("subscribe", subscribe))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("analysis", analysis))
    application.add_handler(CommandHandler("help", start))
    
    # Schedule periodic updates (every 30 minutes)
    job_queue = application.job_queue
    job_queue.run_repeating(send_periodic_updates, interval=1800)
    
    # Start the bot
    await application.run_polling()

if __name__ == "__main__":
    asyncio.run(main())