import os
import requests
from datetime import datetime, timedelta
import random
import pytz
from flask import Flask, render_template
from dotenv import load_dotenv


load_dotenv("data.env")

NASA_API = os.getenv('NASA_API')

app = Flask(__name__)

def format_date(date_str):
    dt = datetime.strptime(date_str, '%Y-%m-%dT%H:%M:%SZ')
    dt = dt.replace(tzinfo=pytz.utc)
    uk_tz = pytz.timezone('Europe/London')
    uk_time = dt.astimezone(uk_tz)
    return uk_time.strftime('%Y %m %d , %H:%M %Z')

def get_apod():
    """get Astronomy Picture of the Day"""
    if not NASA_API:
        print("ERROR: NASA_API key not found in .env")
        return None
    try:
        response = requests.get(
            'https://api.nasa.gov/planetary/apod',
            params={'api_key': NASA_API}
        )
        return response.json()
    except Exception as e:
        print("APOD fetch failed:", e)
        return None
    
def get_epic():
    """The EPIC API provides information on the daily imagery collected 
    by DSCOVR's Earth Polychromatic Imaging Camera (EPIC) instrument"""
    if not NASA_API:
        print("ERROR: NASA_API key not found in .env")
        return None
    try:
        response = requests.get(
            'https://epic.gsfc.nasa.gov/api/natural/images',
            params={'api_key': NASA_API}
        )
        response.raise_for_status()
        images = response.json()
        random.shuffle(images)
        result = []
        for img in images[:2]:
            date = img['date'][:10].replace('-', '/')
            url = f"https://epic.gsfc.nasa.gov/archive/natural/{date}/png/{img['image']}.png"
            result.append({'url': url, 'title': img['caption']})
        return result
    except Exception as e:
        print("EPIC failed:", e)
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
        return asteroids[:5]
    except Exception as e:
        print("NeoWs failed:", e)
        return None
    
def get_launches():
    """Get 5 upcoming rocket launches"""
    try:
        response = requests.get(
            'https://ll.thespacedevs.com/2.3.0/launches/upcoming/',
            params={
                'limit': 5,
                'format': 'json'
            }
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
            })
        return launches
    except Exception as e:
        print("Launches failed:", e)
        return None


@app.route('/')
def home():
    apod = get_apod()
    epic = get_epic()
    neows = get_neows()
    launches = get_launches()
    return render_template("home.html",apod=apod, epic=epic, neows=neows, launches=launches)


if __name__ == "__main__":
    app.run(debug=True)