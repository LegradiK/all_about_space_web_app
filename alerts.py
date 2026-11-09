"""Email alerts for ISS passes and UK aurora.

Visitors sign up on /alerts with their email and closest city, then confirm
by email. A GitHub Actions workflow POSTs to /tasks/check-alerts every
10 minutes; that check sends:

  - ISS alert: a visible pass starts in 15–40 minutes (usually ~30) AND it is
    a good one (high, bright) AND the sky over the city is mostly clear.
  - Aurora alert: AuroraWatch UK's level is high enough for the city's
    latitude AND it is dark there AND the sky is mostly clear.

Settings (data.env):
    SITE_URL          e.g. https://legradik.pythonanywhere.com (for links in emails)
    ALERT_TASK_TOKEN  long random string, also stored as a GitHub secret
    MAIL_USERNAME / MAIL_PASSWORD   see mailer.py
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
EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')


# ───────────────────────── database ─────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_db() as db:
        db.executescript('''
            CREATE TABLE IF NOT EXISTS subscribers (
                id INTEGER PRIMARY KEY,
                email TEXT UNIQUE NOT NULL,
                city TEXT NOT NULL,
                lat REAL NOT NULL,
                lon REAL NOT NULL,
                want_iss INTEGER NOT NULL DEFAULT 1,
                want_aurora INTEGER NOT NULL DEFAULT 1,
                token TEXT UNIQUE NOT NULL,
                confirmed INTEGER NOT NULL DEFAULT 0,
                confirm_sent_at TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sent_alerts (
                subscriber_id INTEGER NOT NULL,
                kind TEXT NOT NULL,
                alert_key TEXT NOT NULL,
                sent_at TEXT NOT NULL,
                PRIMARY KEY (subscriber_id, kind, alert_key)
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

_cities = None


def load_cities():
    global _cities
    if _cities is None:
        with open(os.path.join(BASE_DIR, 'static', 'cities.json'), encoding='utf-8') as f:
            _cities = sorted(set(json.load(f)))
    return _cities


def city_coordinates(name):
    """Lat/lon for a UK city: from the cache, or looked up once with Open-Meteo."""
    with get_db() as db:
        row = db.execute('SELECT lat, lon FROM city_coords WHERE name = ?', (name,)).fetchone()
        if row:
            return row['lat'], row['lon']
    response = requests.get(
        'https://geocoding-api.open-meteo.com/v1/search',
        params={'name': name, 'count': 1, 'language': 'en', 'format': 'json', 'countryCode': 'GB'},
        timeout=10
    )
    response.raise_for_status()
    results = response.json().get('results') or []
    if not results:
        return None
    lat, lon = results[0]['latitude'], results[0]['longitude']
    with get_db() as db:
        db.execute('INSERT OR REPLACE INTO city_coords (name, lat, lon) VALUES (?, ?, ?)', (name, lat, lon))
    return lat, lon


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
                # passes are ~90 minutes apart, so one email per hour is one per pass
                if already_sent(db, s['id'], 'iss', key, within=timedelta(minutes=60)):
                    continue
                unsubscribe_url = site_url(f'/alerts/unsubscribe/{s["token"]}')
                context = dict(
                    sub=s, city=city, minutes=round(minutes), cloud=cloud,
                    start=uk_time(p['start']), end=uk_time(p['end']),
                    start_dir=p['start_dir'], end_dir=p['end_dir'],
                    max_elevation=round(p['max_elevation']), duration=p['duration_min'],
                    magnitude=round(p['magnitude'], 1),
                    iss_url=site_url('/iss'), unsubscribe_url=unsubscribe_url,
                )
                ok = send_email(
                    s['email'],
                    f'ISS visible over {city} at {context["start"]}, look {p["start_dir"]}',
                    render_template('emails/iss_alert.txt', **context),
                    render_template('emails/iss_alert.html', **context),
                    unsubscribe_url,
                )
                if ok:
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
        unsubscribe_url = site_url(f'/alerts/unsubscribe/{s["token"]}')
        context = dict(sub=s, city=s['city'], level=level, cloud=cloud,
                       aurora_url=site_url('/aurora'), unsubscribe_url=unsubscribe_url)
        ok = send_email(
            s['email'],
            f'Aurora alert for {s["city"]}: {level.capitalize()}, sky {cloud}% cloudy',
            render_template('emails/aurora_alert.txt', **context),
            render_template('emails/aurora_alert.html', **context),
            unsubscribe_url,
        )
        if ok:
            record_sent(db, s['id'], 'aurora', f'{night}:{level}')
            sent += 1
    return sent


def check_and_send(now=None):
    now = now or utcnow()
    summary = {'checked_at': now.isoformat(), 'iss_sent': 0, 'aurora_sent': 0, 'errors': []}
    with get_db() as db:
        subs = db.execute('SELECT * FROM subscribers WHERE confirmed = 1').fetchall()
        summary['subscribers'] = len(subs)
        for name, fn in (('iss_sent', check_iss), ('aurora_sent', check_aurora)):
            try:
                summary[name] = fn(db, subs, now)
            except Exception as e:
                print(f"Alert check {name} failed:", e)
                summary['errors'].append(f'{name}: {e}')
        # tidy up old records
        db.execute('DELETE FROM sent_alerts WHERE sent_at < ?', ((now - timedelta(days=7)).isoformat(),))
    return summary


# ───────────────────────── routes ─────────────────────────

MESSAGES = {
    'check-email': 'Nearly there! We have sent you an email. Click the link in it to switch your alerts on.',
    'confirmed': 'Your alerts are on. We will email you when the ISS or the aurora is worth looking up for.',
    'unsubscribed': 'You have been unsubscribed. No more alert emails will be sent.',
    'bad-link': 'That link is not valid any more. You can sign up again below.',
    'resend-wait': 'We sent you a confirmation email a moment ago. Please check your inbox (and spam folder).',
}


@alerts_bp.route('/alerts', methods=['GET'])
def alerts_page():
    message = MESSAGES.get(request.args.get('status'))
    return render_template('alerts.html', cities=load_cities(), message=message,
                           error=None, form={})


@alerts_bp.route('/alerts', methods=['POST'])
def subscribe():
    form = {
        'email': (request.form.get('email') or '').strip().lower(),
        'city': (request.form.get('city') or '').strip(),
        'want_iss': bool(request.form.get('want_iss')),
        'want_aurora': bool(request.form.get('want_aurora')),
    }
    cities = load_cities()
    error = None
    if not EMAIL_RE.match(form['email']):
        error = 'Please enter a valid email address.'
    elif form['city'] not in cities:
        error = 'Please choose your closest city from the list.'
    elif not (form['want_iss'] or form['want_aurora']):
        error = 'Please choose at least one type of alert.'

    coords = None
    if not error:
        try:
            coords = city_coordinates(form['city'])
        except Exception as e:
            print("Geocoding failed:", e)
        if not coords:
            error = 'Sorry, we could not find that city on the map. Please choose another one nearby.'
    if error:
        return render_template('alerts.html', cities=cities, message=None, error=error, form=form), 400

    now = utcnow()
    with get_db() as db:
        existing = db.execute('SELECT * FROM subscribers WHERE email = ?', (form['email'],)).fetchone()
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
                          confirm_sent_at, email, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                       values + (form['email'], now.isoformat()))
        sub = db.execute('SELECT * FROM subscribers WHERE email = ?', (form['email'],)).fetchone()
    send_confirmation(sub)
    return redirect(url_for('alerts.alerts_page', status='check-email'))


@alerts_bp.route('/alerts/confirm/<token>')
def confirm(token):
    with get_db() as db:
        cur = db.execute('UPDATE subscribers SET confirmed = 1 WHERE token = ?', (token,))
    return redirect(url_for('alerts.alerts_page', status='confirmed' if cur.rowcount else 'bad-link'))


@alerts_bp.route('/alerts/unsubscribe/<token>', methods=['GET', 'POST'])
def unsubscribe(token):
    with get_db() as db:
        sub = db.execute('SELECT id FROM subscribers WHERE token = ?', (token,)).fetchone()
        if sub:
            db.execute('DELETE FROM sent_alerts WHERE subscriber_id = ?', (sub['id'],))
            db.execute('DELETE FROM subscribers WHERE id = ?', (sub['id'],))
    return redirect(url_for('alerts.alerts_page', status='unsubscribed' if sub else 'bad-link'))


@alerts_bp.route('/tasks/check-alerts', methods=['POST'])
def run_check():
    expected = os.getenv('ALERT_TASK_TOKEN')
    given = request.headers.get('X-Alert-Token', '')
    if not expected or not hmac.compare_digest(given, expected):
        abort(403)
    return jsonify(check_and_send())


init_db()
