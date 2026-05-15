import os
import requests
from flask import Flask, render_template
from dotenv import load_dotenv

load_dotenv()

NASA_API = os.getenv('NASA_API')

app = Flask(__name__)

def get_apod():
    """get Astronomy Picture of the Day"""
    try:
        response = requests.get(
            'https://api.nasa.gov/planetary/apod',
            params={'api_key': NASA_API}
        )
        return response.json()
    except Exception:
        return None
    

@app.route('/')
def home():
    apod = get_apod()
    return render_template("home.html",apod=apod)


if __name__ == "__main__":
    app.run(debug=True)