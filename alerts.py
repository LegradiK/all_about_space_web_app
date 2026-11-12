"""Email alerts for ISS passes and UK aurora.

Visitors sign up on /alerts with their closest city and choose how to hear
about it: by email (confirmed with a link) or Telegram (confirmed by pressing
Start in the bot). A GitHub Actions workflow POSTs to /tasks/check-alerts every
10 minutes; that check sends:

  - ISS alert: a visible pass starts in 15–40 minutes (usually ~30) AND it is
    a good one (high, bright) AND the sky over the city is mostly clear.
  - Aurora alert: AuroraWatch UK's level is high enough for the city's
    latitude AND it is dark there AND the sky is mostly clear.

Settings (data.env):
    SITE_URL          e.g. https://legradik.pythonanywhere.com (for links in emails)
    ALERT_TASK_TOKEN  long random string, also stored as a GitHub secret
    MAIL_USERNAME / MAIL_PASSWORD   see mailer.py
    TELEGRAM_*        see telegram_bot.py
    ALERT_TEST_EMAIL / ALERT_TEST_TELEGRAM_CHAT_ID
                      your own subscription(s); "Run workflow" with a test option
                      sends made-up alerts to these only
"""
import hmac
import json
import os
import re
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone

import pytz
import requests
from flask import Blueprint, abort, jsonify, redirect, render_template, request, url_for

import iss_passes
import telegram_bot
from mailer import send_email

alerts_bp = Blueprint('alerts', __name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.getenv('ALERTS_DB', os.path.join(BASE_DIR, 'alerts.db'))
UK_TZ = pytz.timezone('Europe/London')

# ISS: what counts as a pass worth an email
ISS_ALERT_WINDOW_MIN = (15, 40)   # minutes before the pass starts
ISS_MIN_ELEVATION = 25            # degrees above the horizon at its highest
ISS_MAX_MAGNITUDE = -1.0          # lower = brighter; -1 is brighter than almost every star
ISS_MAX_CLOUD = 40                # % cloud cover

# Aurora: AuroraWatch UK level needed, by how far north the city is
AURORA_LEVEL_MIN_LAT = {
    'red': 0,       # visible across much of the UK
    'amber': 53.3,  # Scotland, northern England, Northern Ireland
    'yellow': 57.0, # northern Scotland only
}
AURORA_DARK_SUN_ALT = -12         # sun this far below the horizon (nautical darkness)
AURORA_MAX_CLOUD = 50             # % cloud cover

CONFIRM_RESEND_MINUTES = 5
TELEGRAM_PENDING_HOURS = 24   # unused Telegram sign-up links expire after this
EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')


# ───────────────────────── database ─────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


SUBSCRIBERS_SQL = '''
    CREATE TABLE IF NOT EXISTS subscribers (
        id INTEGER PRIMARY KEY,
        channel TEXT NOT NULL DEFAULT 'email',   -- 'email' or 'telegram'
        email TEXT,
        telegram_chat_id INTEGER,
        city TEXT NOT NULL,
        lat REAL NOT NULL,
        lon REAL NOT NULL,
        want_iss INTEGER NOT NULL DEFAULT 1,
        want_aurora INTEGER NOT NULL DEFAULT 1,
        token TEXT UNIQUE NOT NULL,
        confirmed INTEGER NOT NULL DEFAULT 0,
        confirm_sent_at TEXT,
        created_at TEXT NOT NULL
    )
'''


def init_db():
    with get_db() as db:
        columns = [r['name'] for r in db.execute('PRAGMA table_info(subscribers)')]
        if columns and 'channel' not in columns:
            # Older email-only table: rebuild it with the new columns, keeping everyone
            db.execute('ALTER TABLE subscribers RENAME TO subscribers_old')
            db.execute(SUBSCRIBERS_SQL)
            db.execute('''INSERT INTO subscribers (id, channel, email, city, lat, lon, want_iss, want_aurora,
                              token, confirmed, confirm_sent_at, created_at)
                          SELECT id, 'email', email, city, lat, lon, want_iss, want_aurora,
                              token, confirmed, confirm_sent_at, created_at FROM subscribers_old''')
            db.execute('DROP TABLE subscribers_old')
        db.execute(SUBSCRIBERS_SQL)
        db.executescript('''
            CREATE UNIQUE INDEX IF NOT EXISTS idx_subscribers_email
                ON subscribers(email) WHERE channel = 'email';
            CREATE UNIQUE INDEX IF NOT EXISTS idx_subscribers_chat
                ON subscribers(telegram_chat_id) WHERE channel = 'telegram' AND telegram_chat_id IS NOT NULL;
            CREATE TABLE IF NOT EXISTS sent_alerts (
                subscriber_id INTEGER NOT NULL,
                kind TEXT NOT NULL,
                alert_key TEXT NOT NULL,
                sent_at TEXT NOT NULL,
                PRIMARY KEY (subscriber_id, kind, alert_key)
            );
            CREATE TABLE IF NOT EXISTS telegram_outbox (
                id INTEGER PRIMARY KEY,
                chat_id INTEGER NOT NULL,
                text TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS city_coords (
                name TEXT PRIMARY KEY,
                lat REAL NOT NULL,
                lon REAL NOT NULL
            );
        ''')


def utcnow():
    return datetime.now(timezone.utc)


# ───────────────────────── cities ─────────────────────────

_places = None


def load_places():
    """static/cities.json: UK places grouped by region, with coordinates.

    Returns (regions, by_name): regions keeps the file's order for the picker,
    by_name maps a place name to {'name', 'region', 'lat', 'lon'}.
    """
    global _places
    if _places is None:
        with open(os.path.join(BASE_DIR, 'static', 'cities.json'), encoding='utf-8') as f:
            regions = json.load(f)['regions']
        by_name = {c['name']: dict(c, region=r['name']) for r in regions for c in r['cities']}
        _places = (regions, by_name)
    return _places


def city_coordinates(name):
    place = load_places()[1].get(name)
    return (place['lat'], place['lon']) if place else None


# ───────────────────────── outside data ─────────────────────────

def cloud_cover_at(lat, lon, when, _cache={}):
    """Forecast cloud cover (%) for the hour containing `when`. None if unavailable."""
    key = (round(lat, 2), round(lon, 2), when.strftime('%Y-%m-%dT%H'))
    if key in _cache:
        return _cache[key]
    try:
        response = requests.get(
            'https://api.open-meteo.com/v1/forecast',
            params={'latitude': lat, 'longitude': lon, 'hourly': 'cloud_cover',
                    'forecast_days': 2, 'timezone': 'UTC'},
            timeout=10
        )
        response.raise_for_status()
        hourly = response.json()['hourly']
        for t, c in zip(hourly['time'], hourly['cloud_cover']):
            _cache[(key[0], key[1], t[:13])] = c
    except Exception as e:
        print("Weather fetch failed:", e)
        return None
    if len(_cache) > 5000:   # keep memory small on a long-running server
        _cache.clear()
    return _cache.get(key)


def aurorawatch_level():
    """Current AuroraWatch UK level: green / yellow / amber / red."""
    response = requests.get(
        'https://aurorawatch-api.lancs.ac.uk/0.2.5/status/alerting-site-activity.xml', timeout=10
    )
    response.raise_for_status()
    order = ['green', 'yellow', 'amber', 'red']
    levels = re.findall(r'status_id="(\w+)"', response.text)
    levels = [l for l in levels if l in order]
    return max(levels, key=order.index) if levels else 'green'


# ───────────────────────── emails ─────────────────────────

def site_url(path):
    base = os.getenv('SITE_URL') or request.host_url
    return base.rstrip('/') + path


def uk_time(dt):
    return dt.astimezone(UK_TZ).strftime('%H:%M')


def send_confirmation(sub):
    confirm_url = site_url(f'/alerts/confirm/{sub["token"]}')
    unsubscribe_url = site_url(f'/alerts/unsubscribe/{sub["token"]}')
    context = dict(sub=sub, confirm_url=confirm_url, unsubscribe_url=unsubscribe_url)
    return send_email(
        sub['email'],
        'Confirm your Horizon Intelligence sky alerts',
        render_template('emails/confirm.txt', **context),
        render_template('emails/confirm.html', **context),
        unsubscribe_url,
    )


def delete_subscriber(db, sub_id):
    db.execute('DELETE FROM sent_alerts WHERE subscriber_id = ?', (sub_id,))
    db.execute('DELETE FROM subscribers WHERE id = ?', (sub_id,))


def notify(db, sub, kind, subject, context):
    """Send one alert by the subscriber's channel. Returns True if it went out.

    kind is the template name: emails/<kind>.html + .txt, or telegram/<kind>.html
    """
    unsubscribe_url = site_url(f'/alerts/unsubscribe/{sub["token"]}')
    context = dict(context, sub=sub, unsubscribe_url=unsubscribe_url)
    if sub['channel'] == 'telegram':
        text = render_template(f'telegram/{kind}.html', **context)
        mode = telegram_bot.delivery_mode()
        if mode != 'github':
            try:
                telegram_bot.send_message(sub['telegram_chat_id'], text)
                return True
            except telegram_bot.ChatGone:
                delete_subscriber(db, sub['id'])   # they blocked the bot, so stop for good
                return False
            except telegram_bot.TelegramUnreachable as e:
                if mode == 'direct':
                    print(f"Telegram unreachable:", e)
                    return False
                # fall through: let the GitHub Action deliver it
            except Exception as e:
                print(f"Telegram to {sub['telegram_chat_id']} failed:", e)
                return False
        # Queued here, handed to the GitHub Action in this check's response
        db.execute('INSERT INTO telegram_outbox (chat_id, text, created_at) VALUES (?, ?, ?)',
                   (sub['telegram_chat_id'], text, utcnow().isoformat()))
        return True
    return send_email(sub['email'], subject,
                      render_template(f'emails/{kind}.txt', **context),
                      render_template(f'emails/{kind}.html', **context),
                      unsubscribe_url)


def already_sent(db, sub_id, kind, key=None, within=None):
    if key is not None:
        row = db.execute('SELECT 1 FROM sent_alerts WHERE subscriber_id=? AND kind=? AND alert_key=?',
                         (sub_id, kind, key)).fetchone()
        if row:
            return True
    if within is not None:
        since = (utcnow() - within).isoformat()
        row = db.execute('SELECT 1 FROM sent_alerts WHERE subscriber_id=? AND kind=? AND sent_at>=?',
                         (sub_id, kind, since)).fetchone()
        if row:
            return True
    return False


def record_sent(db, sub_id, kind, key):
    db.execute('INSERT OR IGNORE INTO sent_alerts VALUES (?, ?, ?, ?)',
               (sub_id, kind, key, utcnow().isoformat()))


# ───────────────────────── the check ─────────────────────────

def check_iss(db, subs, now):
    sent = 0
    by_city = {}
    for s in subs:
        if s['want_iss']:
            by_city.setdefault((s['city'], s['lat'], s['lon']), []).append(s)

    lo, hi = ISS_ALERT_WINDOW_MIN
    for (city, lat, lon), people in by_city.items():
        for p in iss_passes.visible_passes(lat, lon, start=now, hours=hi / 60):
            minutes = (p['start'] - now).total_seconds() / 60
            if not lo <= minutes <= hi:
                continue
            if p['max_elevation'] < ISS_MIN_ELEVATION or p['magnitude'] > ISS_MAX_MAGNITUDE:
                continue
            cloud = cloud_cover_at(lat, lon, p['max_time'])
            if cloud is None or cloud > ISS_MAX_CLOUD:
                continue

            key = p['start'].strftime('%Y-%m-%dT%H:%M')
            for s in people:
                # passes are ~90 minutes apart, so one alert per hour is one per pass
                if already_sent(db, s['id'], 'iss', key, within=timedelta(minutes=60)):
                    continue
                context = dict(
                    city=city, minutes=round(minutes), cloud=cloud,
                    start=uk_time(p['start']), end=uk_time(p['end']),
                    start_dir=p['start_dir'], end_dir=p['end_dir'],
                    max_elevation=round(p['max_elevation']), duration=p['duration_min'],
                    magnitude=round(p['magnitude'], 1), iss_url=site_url('/iss'),
                )
                subject = f'ISS visible over {city} at {context["start"]}, look {p["start_dir"]}'
                if notify(db, s, 'iss_alert', subject, context):
                    record_sent(db, s['id'], 'iss', key)
                    sent += 1
    return sent


def check_aurora(db, subs, now):
    people = [s for s in subs if s['want_aurora']]
    if not people:
        return 0
    level = aurorawatch_level()
    if level not in AURORA_LEVEL_MIN_LAT:
        return 0

    # One email per night per level, so it only repeats if the storm gets stronger
    night = (now.astimezone(UK_TZ) - timedelta(hours=12)).strftime('%Y-%m-%d')
    order = ['yellow', 'amber', 'red']
    sent = 0
    for s in people:
        if s['lat'] < AURORA_LEVEL_MIN_LAT[level]:
            continue
        if iss_passes.sun_altitude(s['lat'], s['lon'], now) > AURORA_DARK_SUN_ALT:
            continue
        cloud = cloud_cover_at(s['lat'], s['lon'], now)
        if cloud is None or cloud > AURORA_MAX_CLOUD:
            continue
        # skip if tonight's email was already for this level or a stronger one
        if any(already_sent(db, s['id'], 'aurora', f'{night}:{l}')
               for l in order[order.index(level):]):
            continue
        context = dict(city=s['city'], level=level, cloud=cloud, aurora_url=site_url('/aurora'))
        subject = f'Aurora alert for {s["city"]}: {level.capitalize()}, sky {cloud}% cloudy'
        if notify(db, s, 'aurora_alert', subject, context):
            record_sent(db, s['id'], 'aurora', f'{night}:{level}')
            sent += 1
    return sent


def test_recipients(db):
    """Your own subscriptions, picked by ALERT_TEST_EMAIL / ALERT_TEST_TELEGRAM_CHAT_ID in data.env.

    Test alerts only ever go to these, never to public subscribers.
    """
    email = (os.getenv('ALERT_TEST_EMAIL') or '').strip().lower()
    chat_id = (os.getenv('ALERT_TEST_TELEGRAM_CHAT_ID') or '').strip()
    subs = []
    if email:
        subs += db.execute("SELECT * FROM subscribers WHERE confirmed = 1 AND channel = 'email' AND email = ?",
                           (email,)).fetchall()
    if chat_id.lstrip('-').isdigit():
        subs += db.execute("SELECT * FROM subscribers WHERE confirmed = 1 AND channel = 'telegram' "
                           "AND telegram_chat_id = ?", (int(chat_id),)).fetchall()
    return subs


def send_test_alerts(db, kinds, now):
    """Send made-up ISS / aurora alerts through the real templates and channels.

    Nothing is recorded in sent_alerts, so real alerts are not affected.
    """
    subs = test_recipients(db)
    sent = {'iss': 0, 'aurora': 0}
    for s in subs:
        if 'iss' in kinds:
            start = now + timedelta(minutes=30)
            context = dict(
                city=s['city'], minutes=30, cloud=10,
                start=uk_time(start), end=uk_time(start + timedelta(minutes=6)),
                start_dir='WSW', end_dir='ENE', max_elevation=72, duration=6,
                magnitude=-3.4, iss_url=site_url('/iss'),
            )
            subject = f'[TEST] ISS visible over {s["city"]} at {context["start"]}, look WSW'
            if notify(db, s, 'iss_alert', subject, context):
                sent['iss'] += 1
        if 'aurora' in kinds:
            context = dict(city=s['city'], level='red', cloud=10, aurora_url=site_url('/aurora'))
            subject = f'[TEST] Aurora alert for {s["city"]}: Red, sky 10% cloudy'
            if notify(db, s, 'aurora_alert', subject, context):
                sent['aurora'] += 1
    return len(subs), sent


def check_and_send(now=None, test=None):
    """Run the real checks, or with test='iss' / 'aurora' / 'both' send test alerts to you only."""
    now = now or utcnow()
    summary = {'checked_at': now.isoformat(), 'iss_sent': 0, 'aurora_sent': 0, 'errors': []}
    with get_db() as db:
        if test:
            kinds = ('iss', 'aurora') if test == 'both' else (test,)
            try:
                summary['test_recipients'], sent = send_test_alerts(db, kinds, now)
                summary['iss_sent'], summary['aurora_sent'] = sent['iss'], sent['aurora']
                if not summary['test_recipients']:
                    summary['errors'].append('test: no confirmed subscription matches ALERT_TEST_EMAIL '
                                             'or ALERT_TEST_TELEGRAM_CHAT_ID in data.env')
            except Exception as e:
                print("Test alert failed:", e)
                summary['errors'].append(f'test: {e}')
        else:
            subs = db.execute('SELECT * FROM subscribers WHERE confirmed = 1').fetchall()
            summary['subscribers'] = len(subs)
            for name, fn in (('iss_sent', check_iss), ('aurora_sent', check_aurora)):
                try:
                    summary[name] = fn(db, subs, now)
                except Exception as e:
                    print(f"Alert check {name} failed:", e)
                    summary['errors'].append(f'{name}: {e}')
        # tidy up old records and Telegram sign-ups nobody finished
        db.execute('DELETE FROM sent_alerts WHERE sent_at < ?', ((now - timedelta(days=7)).isoformat(),))
        db.execute("DELETE FROM subscribers WHERE channel = 'telegram' AND confirmed = 0 AND created_at < ?",
                   ((now - timedelta(hours=TELEGRAM_PENDING_HOURS)).isoformat(),))

        # Telegram messages the site couldn't send itself: the GitHub Action sends these.
        # Anything older than an hour is dropped, since an alert that late is no use.
        outbox = db.execute('SELECT id, chat_id, text, created_at FROM telegram_outbox ORDER BY id').fetchall()
        fresh = (now - timedelta(hours=1)).isoformat()
        summary['telegram_outbox'] = [{'chat_id': r['chat_id'], 'text': r['text']}
                                      for r in outbox if r['created_at'] >= fresh]
        db.execute('DELETE FROM telegram_outbox')
    return summary


# ───────────────────────── routes ─────────────────────────

MESSAGES = {
    'check-email': 'Nearly there! We have sent you an email. Click the link in it to switch your alerts on.',
    'confirmed': 'Your alerts are on. We will email you when the ISS or the aurora is worth looking up for.',
    'unsubscribed': 'You have been unsubscribed. No more alerts will be sent.',
    'bad-link': 'That link is not valid any more. You can sign up again below.',
    'resend-wait': 'We sent you a confirmation email a moment ago. Please check your inbox (and spam folder).',
}


@alerts_bp.route('/alerts', methods=['GET'])
def alerts_page():
    message = MESSAGES.get(request.args.get('status'))
    return render_template('alerts.html', regions=load_places()[0], message=message,
                           error=None, form={}, telegram_enabled=telegram_bot.is_configured())


@alerts_bp.route('/alerts', methods=['POST'])
def subscribe():
    telegram_enabled = telegram_bot.is_configured()
    form = {
        'channel': 'telegram' if telegram_enabled and request.form.get('channel') == 'telegram' else 'email',
        'email': (request.form.get('email') or '').strip().lower(),
        'city': (request.form.get('city') or '').strip(),
        'want_iss': bool(request.form.get('want_iss')),
        'want_aurora': bool(request.form.get('want_aurora')),
    }
    regions, places = load_places()
    # accept any capitalisation, but store the official spelling
    form['city'] = next((n for n in places if n.lower() == form['city'].lower()), form['city'])
    error = None
    if form['channel'] == 'email' and not EMAIL_RE.match(form['email']):
        error = 'Please enter a valid email address.'
    elif form['city'] not in places:
        error = 'Please choose your closest city or town from the list.'
    elif not (form['want_iss'] or form['want_aurora']):
        error = 'Please choose at least one type of alert.'

    coords = city_coordinates(form['city']) if not error else None
    if error:
        return render_template('alerts.html', regions=regions, message=None, error=error, form=form,
                               telegram_enabled=telegram_enabled), 400

    now = utcnow()
    if form['channel'] == 'telegram':
        # Saved as pending until the user presses Start in the bot (see telegram_webhook)
        token = secrets.token_urlsafe(24)
        with get_db() as db:
            db.execute('''INSERT INTO subscribers (channel, city, lat, lon, want_iss, want_aurora,
                          token, created_at) VALUES ('telegram', ?, ?, ?, ?, ?, ?, ?)''',
                       (form['city'], coords[0], coords[1], int(form['want_iss']),
                        int(form['want_aurora']), token, now.isoformat()))
        return redirect(url_for('alerts.telegram_connect', token=token))

    with get_db() as db:
        existing = db.execute("SELECT * FROM subscribers WHERE channel = 'email' AND email = ?",
                              (form['email'],)).fetchone()
        if existing and existing['confirm_sent_at'] and \
                now - datetime.fromisoformat(existing['confirm_sent_at']) < timedelta(minutes=CONFIRM_RESEND_MINUTES):
            return redirect(url_for('alerts.alerts_page', status='resend-wait'))
        # New settings always need confirming by email, so nobody can change someone else's alerts
        token = secrets.token_urlsafe(24)
        values = (form['city'], coords[0], coords[1], int(form['want_iss']), int(form['want_aurora']),
                  token, now.isoformat())
        if existing:
            db.execute('''UPDATE subscribers SET city=?, lat=?, lon=?, want_iss=?, want_aurora=?,
                          token=?, confirmed=0, confirm_sent_at=? WHERE id=?''', values + (existing['id'],))
        else:
            db.execute('''INSERT INTO subscribers (city, lat, lon, want_iss, want_aurora, token,
                          confirm_sent_at, email, channel, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'email', ?)''',
                       values + (form['email'], now.isoformat()))
        sub = db.execute("SELECT * FROM subscribers WHERE channel = 'email' AND email = ?",
                         (form['email'],)).fetchone()
    send_confirmation(sub)
    return redirect(url_for('alerts.alerts_page', status='check-email'))


@alerts_bp.route('/alerts/confirm/<token>')
def confirm(token):
    with get_db() as db:
        cur = db.execute("UPDATE subscribers SET confirmed = 1 WHERE token = ? AND channel = 'email'", (token,))
    return redirect(url_for('alerts.alerts_page', status='confirmed' if cur.rowcount else 'bad-link'))


@alerts_bp.route('/alerts/unsubscribe/<token>', methods=['GET', 'POST'])
def unsubscribe(token):
    with get_db() as db:
        sub = db.execute('SELECT id FROM subscribers WHERE token = ?', (token,)).fetchone()
        if sub:
            delete_subscriber(db, sub['id'])
    return redirect(url_for('alerts.alerts_page', status='unsubscribed' if sub else 'bad-link'))


@alerts_bp.route('/tasks/check-alerts', methods=['POST'])
def run_check():
    expected = os.getenv('ALERT_TASK_TOKEN')
    given = request.headers.get('X-Alert-Token', '')
    if not expected or not hmac.compare_digest(given, expected):
        abort(403)
    test = (request.get_json(silent=True) or {}).get('test')
    if test not in ('iss', 'aurora', 'both'):
        test = None
    return jsonify(check_and_send(test=test))


init_db()

_missing = telegram_bot.missing_settings()
if _missing:
    print(f"Sky Alerts: Telegram is OFF (missing in data.env: {', '.join(_missing)}). Email only.")
else:
    print("Sky Alerts: Telegram is ON.")


# ───────────────────────── Telegram ─────────────────────────

@alerts_bp.route('/alerts/telegram/<token>')
def telegram_connect(token):
    """Page with the "Open Telegram" button for a pending Telegram sign-up."""
    with get_db() as db:
        sub = db.execute("SELECT * FROM subscribers WHERE token = ? AND channel = 'telegram'",
                         (token,)).fetchone()
    if not sub:
        return redirect(url_for('alerts.alerts_page', status='bad-link'))
    return render_template('alerts_telegram.html', sub=sub, link=telegram_bot.start_link(token))


def _reply(chat_id, template, **context):
    """Reply in the webhook response, so it works even when we can't reach api.telegram.org."""
    html = render_template(f'telegram/{template}.html', alerts_url=site_url('/alerts'), **context)
    return jsonify(telegram_bot.webhook_reply(chat_id, html))


@alerts_bp.route('/telegram/webhook', methods=['POST'])
def telegram_webhook():
    """Telegram calls this for every message sent to the bot."""
    expected = os.getenv('TELEGRAM_WEBHOOK_SECRET')
    given = request.headers.get('X-Telegram-Bot-Api-Secret-Token', '')
    if not expected or not hmac.compare_digest(given, expected):
        abort(403)

    message = (request.get_json(silent=True) or {}).get('message') or {}
    chat_id = (message.get('chat') or {}).get('id')
    text = (message.get('text') or '').strip()
    if not chat_id:
        return 'ok'
    if not text.startswith('/'):
        return _reply(chat_id, 'help')

    command, _, arg = text.partition(' ')
    command = command.split('@')[0].lower()   # "/start@HorizonIntelBot" in group chats

    with get_db() as db:
        current = db.execute("SELECT * FROM subscribers WHERE channel = 'telegram' AND telegram_chat_id = ?",
                             (chat_id,)).fetchone()

        if command == '/start' and arg:
            pending = db.execute("SELECT * FROM subscribers WHERE token = ? AND channel = 'telegram'",
                                 (arg.strip(),)).fetchone()
            # a link already used by a different chat can't be taken over
            if not pending or (pending['telegram_chat_id'] and pending['telegram_chat_id'] != chat_id):
                return _reply(chat_id, 'bad_link')
            # One subscription per chat: new settings replace any old ones
            if current and current['id'] != pending['id']:
                delete_subscriber(db, current['id'])
            db.execute('UPDATE subscribers SET telegram_chat_id = ?, confirmed = 1 WHERE id = ?',
                       (chat_id, pending['id']))
            sub = db.execute('SELECT * FROM subscribers WHERE id = ?', (pending['id'],)).fetchone()
            return _reply(chat_id, 'welcome', sub=sub)
        if command == '/stop':
            if current:
                delete_subscriber(db, current['id'])
            return _reply(chat_id, 'stopped')
        if command == '/status':
            return _reply(chat_id, 'status', sub=current)
    return _reply(chat_id, 'help')


@alerts_bp.route('/tasks/telegram-gone', methods=['POST'])
def telegram_gone():
    """The GitHub Action reports chats that blocked the bot, so we stop messaging them."""
    expected = os.getenv('ALERT_TASK_TOKEN')
    given = request.headers.get('X-Alert-Token', '')
    if not expected or not hmac.compare_digest(given, expected):
        abort(403)
    chat_ids = [int(c) for c in (request.get_json(silent=True) or {}).get('chat_ids', [])]
    removed = 0
    with get_db() as db:
        for chat_id in chat_ids:
            for row in db.execute("SELECT id FROM subscribers WHERE channel = 'telegram' AND telegram_chat_id = ?",
                                  (chat_id,)).fetchall():
                delete_subscriber(db, row['id'])
                removed += 1
    return jsonify({'removed': removed})