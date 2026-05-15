import requests
from flask import Flask, render_template

app = Flask(__name__)

def index(request):
    apod = None
    try:
        response = requests.get(
            'https://api.nasa.gov/planetary/apod',
            params={'api_key': 'YOUR_API_KEY'}
        )
        apod = response.json()
    except Exception:
        pass

    return render(request, 'index.html', {'apod': apod})

@app.route('/')
def home():
    return render_template("home.html")


if __name__ == "__main__":
    app.run(debug=True)