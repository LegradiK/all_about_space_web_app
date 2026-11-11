"""One-off setup: tell Telegram where to deliver messages sent to the bot.

Run once after deploying (e.g. from a PythonAnywhere Bash console), and again
if SITE_URL or TELEGRAM_WEBHOOK_SECRET change:

    python telegram_setup.py
"""
import os

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data.env'))

import telegram_bot  # noqa: E402  (needs the settings loaded first)


def main():
    site = os.environ['SITE_URL'].rstrip('/')
    secret = os.environ['TELEGRAM_WEBHOOK_SECRET']

    telegram_bot.call('setWebhook', url=f'{site}/telegram/webhook', secret_token=secret,
                      allowed_updates=['message'], drop_pending_updates=True)
    telegram_bot.call('setMyCommands', commands=[
        {'command': 'status', 'description': 'Show your alert settings'},
        {'command': 'stop', 'description': 'Stop all alerts'},
        {'command': 'help', 'description': 'How the alerts work'},
    ])
    info = telegram_bot.call('getWebhookInfo')
    print('Webhook set:', info.get('result', {}).get('url'))


if __name__ == '__main__':
    main()