"""Send emails through Gmail (or any SMTP server) using settings from data.env.

    MAIL_USERNAME   your Gmail address
    MAIL_PASSWORD   a Gmail *app password* (Google Account → Security → App passwords)
    MAIL_FROM       optional display sender, e.g. "Horizon Intelligence <you@gmail.com>"
    MAIL_SERVER     optional, default smtp.gmail.com (port 465, SSL)

If MAIL_USERNAME is not set, emails are printed to the console instead,
which is handy when running locally.
"""
import os
import smtplib
from email.message import EmailMessage


def send_email(to, subject, text, html=None, unsubscribe_url=None):
    username = os.getenv('MAIL_USERNAME')
    password = os.getenv('MAIL_PASSWORD')
    sender = os.getenv('MAIL_FROM') or f'Horizon Intelligence <{username}>'

    msg = EmailMessage()
    msg['Subject'] = subject
    msg['From'] = sender
    msg['To'] = to
    if unsubscribe_url:
        msg['List-Unsubscribe'] = f'<{unsubscribe_url}>'
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype='html')

    if not username or not password:
        print(f"[mail not configured] To: {to}\nSubject: {subject}\n{text}\n")
        return True

    try:
        with smtplib.SMTP_SSL(os.getenv('MAIL_SERVER', 'smtp.gmail.com'), 465, timeout=20) as smtp:
            smtp.login(username, password)
            smtp.send_message(msg)
        return True
    except Exception as e:
        print(f"Email to {to} failed:", e)
        return False
