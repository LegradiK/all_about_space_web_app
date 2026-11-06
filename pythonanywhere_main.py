import os
import re
import html
import requests
from datetime import datetime, timedelta
import random
import pytz
from flask import Flask, render_template, Response
from dotenv import load_dotenv


def rocketlaunch_url(name):
    slug = name.lower()
    slug = re.sub(r'\s*\|\s*', '-', slug)
    slug = slug.replace(' ', '-')
    slug = re.sub(r'[^a-z0-9-]', '', slug)
    slug = re.sub(r'-+', '-', slug).strip('-')
    return f'https://rocketlaunch.org/mission-{slug}'


load_dotenv("data.env")

NASA_API = os.getenv('NASA_API')

app = Flask(__name__)

def format_date(date_str):
    dt = datetime.strptime(date_str, '%Y-%m-%dT%H:%M:%SZ')
    dt = dt.replace(tzinfo=pytz.utc)
    uk_tz = pytz.timezone('Europe/London')
    uk_time = dt.astimezone(uk_tz)
    return uk_time.strftime('%Y %m %d , %H:%M %Z')

def strip_html(text):
    # The new APOD API returns explanation/credit/copyright as HTML fragments
    text = re.sub(r'<[^>]+>', '', text or '')
    return html.unescape(text).strip()


def split_first_sentence(text):
    # Split after the first . ! or ? that is followed by a space and a capital letter,
    # so abbreviations like "e.g. the" don't cut the sentence short
    parts = re.split(r'(?<=[.!?])\s+(?=[A-Z"\u201c(])', text or '', maxsplit=1)
    first = parts[0].strip()
    rest = parts[1].strip() if len(parts) > 1 else ''
    return first, rest


def get_apod():
    # APOD moved to science.nasa.gov on 2026-09-29. The old
    # api.nasa.gov/planetary/apod endpoint now returns a placeholder
    # (NASA logo, "NASA Science") or 503/504, and retires on 2026-12-01.
    # The new endpoint needs no API key.
    try:
        response = requests.get(
            'https://science.nasa.gov/wp-json/wp/v2/apod-basic',
            params={
                'per_page': 1,
                '_fields': 'date,title,media_type,explanation,credit,copyright,alt,url,hdurl,permalink'
            },
            timeout=15
        )
        response.raise_for_status()
        data = response.json()
        if not data:
            print("APOD: empty response")
            return None
        item = data[0]  # the endpoint returns a list, newest first
        explanation = strip_html(item.get('explanation'))
        summary, more = split_first_sentence(explanation)
        return {
            'date': item.get('date'),
            'title': strip_html(item.get('title')),
            'media_type': item.get('media_type'),
            'explanation': explanation,
            'summary': summary,   # first sentence, shown by default
            'more': more,         # the rest, revealed by "Read more"
            'copyright': strip_html(item.get('copyright') or item.get('credit')),
            'alt': item.get('alt'),
            # 'url' is now the article page, 'hdurl' is the actual image
            'url': item.get('hdurl'),
            'hdurl': item.get('hdurl'),
            'permalink': item.get('permalink') or item.get('url'),
            'source_url': item.get('hdurl') or item.get('permalink'),
        }
    except Exception as e:
        print("APOD fetch failed:", e)
        return None

def get_epic():
    # Call the EPIC server directly (no API key needed) instead of
    # going through the api.nasa.gov gateway
    try:
        response = requests.get(
            'https://epic.gsfc.nasa.gov/api/natural',
            timeout=15
        )
        response.raise_for_status()
        images = response.json()
        if not images:
            print("EPIC: no images returned")
            return None
        random.shuffle(images)
        result = []
        for img in images[:2]:
            date = img['date'][:10].replace('-', '/')
            url = f"https://epic.gsfc.nasa.gov/archive/natural/{date}/jpg/{img['image']}.jpg"
            result.append({'url': url, 'title': img['caption']})
        return result
    except Exception as e:
        print("EPIC failed:", e)
        return None

def get_iss_position():
    if not NASA_API:
        print("ERROR: NASA_API key not found in .env")
        return None
    try:
        response = requests.get(
            "https://api.wheretheiss.at/v1/satellites/25544",
            headers={"User-Agent": "space-dashboard/1.0"},
            timeout=10,
        )    
        response.raise_for_status()
        data = response.json()
        return {
            "lat": data["latitude"],
            "lon": data["longitude"],
            "alt_km": data["altitude"],
            "speed_kmh": data["velocity"],
            "visibility": data["visibility"],
            "timestamp": data["timestamp"],
        }
    except Exception as e:
        print("ISS tracking failed:", e)
        return None

def get_neows():
    if not NASA_API:
        print("ERROR: NASA_API key not found in .env")
        return None
    try:
        today = datetime.today().date()
        search_end_day = today + timedelta(days=7)
        response = requests.get(
            "https://api.nasa.gov/neo/rest/v1/feed",
            params={
                "start_date": str(today),
                "end_date": str(search_end_day),
                "api_key": NASA_API
            }
        )
        response.raise_for_status()
        data = response.json()
        asteroids = []
        for date in data['near_earth_objects']:
            for r in data['near_earth_objects'][date]:
                asteroids.append({
                    'name': r['name'],
                    'date': r['close_approach_data'][0]['close_approach_date'],
                    'distance': round(float(r['close_approach_data'][0]['miss_distance']['kilometers'])),
                    'dangerous': r['is_potentially_hazardous_asteroid'],
                    'url': r['nasa_jpl_url']
                })
        random.shuffle(asteroids)
        asteroids.sort(key=lambda x: x['date'])
        return asteroids[:10]
    except Exception as e:
        print("NeoWs failed:", e)
        return None

def get_launches():
    try:
        response = requests.get(
            'https://ll.thespacedevs.com/2.3.0/launches/upcoming/',
            params={'limit': 5, 'format': 'json'}
        )
        response.raise_for_status()
        data = response.json()
        launches = []
        for r in data['results']:
            launches.append({
                'name': r['name'],
                'date': format_date(r['net']),
                'status': r['status']['name'],
                'location': r['pad']['location']['name'],
                'url': rocketlaunch_url(r['name']),
                'image': r.get('image'),
                'mission': r['mission']['description'] if r.get('mission') else None
            })
        return launches
    except Exception as e:
        print("Launches failed:", e)
        return None

@app.route('/api/aurora-status')
def aurora_proxy():
    r = requests.get('https://aurorawatch-api.lancs.ac.uk/0.2.5/status/alerting-site-activity.xml')
    return Response(r.content, content_type='application/xml')

@app.route('/')
def home():
    apod = get_apod()
    epic = get_epic()
    return render_template("home.html", apod=apod, epic=epic)

@app.route('/iss')
def iss_page():
    iss_tracking = get_iss_position()
    return render_template("iss.html", iss=iss_tracking)

@app.route('/launches')
def launches_page():
    launches = get_launches()
    return render_template("launches.html", launches=launches)

@app.route('/asteroids')
def asteroids_page():
    neows = get_neows()
    return render_template("asteroids.html", neows=neows)

@app.route('/aurora')
def aurora_page():
    return render_template("aurora.html")


if __name__ == "__main__":
    app.run(debug=True)