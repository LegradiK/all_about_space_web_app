"""Send Telegram messages from the Horizon Intelligence bot.

Settings (data.env):
    TELEGRAM_BOT_TOKEN       from @BotFather, e.g. 123456:ABC-DEF...
    TELEGRAM_BOT_USERNAME    the bot's username without @, e.g. HorizonIntelBot
    TELEGRAM_WEBHOOK_SECRET  long random string; Telegram sends it back with every update

If TELEGRAM_BOT_TOKEN is not set, messages are printed to the console instead.
"""
import os

import requests

API = 'https://api.telegram.org/bot{token}/{method}'


class ChatGone(Exception):
    """The user blocked the bot or deleted the chat, so stop messaging them."""


def is_configured():
    return bool(os.getenv('TELEGRAM_BOT_TOKEN') and os.getenv('TELEGRAM_BOT_USERNAME'))


def start_link(token):
    """Deep link that opens the bot and sends /start <token> when the user presses Start."""
    return f'https://t.me/{os.getenv("TELEGRAM_BOT_USERNAME")}?start={token}'


def call(method, **payload):
    token = os.getenv('TELEGRAM_BOT_TOKEN')
    if not token:
        print(f"[telegram not configured] {method}: {payload}")
        return {'ok': True}
    response = requests.post(API.format(token=token, method=method), json=payload, timeout=15)
    data = response.json()
    if not data.get('ok'):
        # 403 = blocked by the user, 400 "chat not found" = chat deleted
        if response.status_code == 403 or 'chat not found' in data.get('description', ''):
            raise ChatGone(data.get('description'))
        raise RuntimeError(f"Telegram {method} failed: {data.get('description')}")
    return data


def send_message(chat_id, html):
    """Send a message using Telegram's HTML formatting (<b>, <i>, <a href>)."""
    return call('sendMessage', chat_id=chat_id, text=html, parse_mode='HTML',
                link_preview_options={'is_disabled': True})
