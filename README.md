# Horizon Intelligence

A space-themed live data dashboard built with Flask. Aggregates real-time data from several public space APIs into a clean, readable interface with dark and light mode support.

---

## Pages

| Page | What it shows |
|---|---|
| **Home** | NASA Astronomy Picture of the Day + two random Earth images from NASA EPIC |
| **ISS Tracker** | Live International Space Station position on an interactive D3 globe, updated every 5 seconds |
| **Aurora Watch** | Real-time geomagnetic activity across UK monitoring sites, sourced from AuroraWatch Lancaster |
| **Asteroid Surveillance** | The 10 nearest asteroid close approaches in the next 7 days from NASA NeoWs |
| **Launch Operations** | Next 5 upcoming rocket launches with mission descriptions, linked to RocketLaunch.org |

---

## Tech Stack

- **Backend** — Python 3, Flask, Requests, python-dotenv, pytz
- **Templating** — Jinja2 (`base.html` → page templates)
- **Frontend** — Vanilla JS, D3.js v7, TopoJSON v3
- **Styling** — Plain CSS with custom properties, dark/light mode via `data-theme` attribute

No database, no task queue, no frontend framework.

---

## External APIs

| API | Used for | Auth |
|---|---|---|
| [NASA APOD](https://api.nasa.gov/) | Astronomy Picture of the Day | API key |
| [NASA EPIC](https://epic.gsfc.nasa.gov/) | Earth Polychromatic Imaging Camera photos | API key |
| [NASA NeoWs](https://api.nasa.gov/) | Near-Earth asteroid close approaches | API key |
| [The Space Devs](https://thespacedevs.com/) | Upcoming rocket launches | None (public) |
| [AuroraWatch UK](https://aurorawatch.lancs.ac.uk/) | Geomagnetic activity at UK sites | None (public XML) |
| [Open Notify](http://api.open-notify.org/) | ISS real-time position (polled from browser) | None |
| [world-atlas](https://cdn.jsdelivr.net/npm/world-atlas@2/) | Country boundaries TopoJSON for maps | None (CDN) |

---

## Setup

### 1. Clone and create a virtual environment

```bash
git clone <repo-url>
cd all_about_space_web_app
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
```

### 2. Install dependencies

```bash
pip install flask requests python-dotenv pytz
```

### 3. Add your NASA API key

Create a file called `data.env` in the project root:

```
NASA_API=your_key_here
```

Get a free key at [https://api.nasa.gov/](https://api.nasa.gov/). The app will still run without it but NASA-dependent pages will show "Data unavailable".

### 4. Run

```bash
python main.py
```

Opens at `http://127.0.0.1:5000` with Flask debug mode enabled.

---

## Project Structure

```
all_about_space_web_app/
├── main.py               # Flask app — all routes and API fetch functions
├── data.env              # NASA API key (gitignored)
├── static/
│   └── style.css         # All styles, CSS custom properties, dark/light theme
└── templates/
    ├── base.html         # Shell: top header, sidebar, footer includes, theme toggle
    ├── sidebar.html      # Side navigation
    ├── footer.html       # Fixed footer
    ├── home.html         # APOD + EPIC page
    ├── iss.html          # ISS tracker (D3 orthographic globe)
    ├── aurora.html       # Aurora Watch (D3 Mercator UK map)
    ├── asteroids.html    # Asteroid close-approach list
    └── launches.html     # Upcoming launch list
```

---

## Architecture Notes

**All API calls are synchronous and happen on each page load** — there is no caching or background refresh on the server side. If an external API is slow or down, that page will be slow or show a fallback message.

**The ISS position is fetched entirely in the browser** — `iss.html` polls `open-notify.org` every 5 seconds via `fetch()` and animates the D3 globe accordingly. The server is not involved after the initial page render.

**Aurora data goes through a Flask proxy** — `GET /api/aurora-status` forwards to the AuroraWatch XML endpoint to avoid browser CORS restrictions. The frontend then parses the XML itself.

**Launch URLs are slugified to RocketLaunch.org** — the `rocketlaunch_url()` function in `main.py` converts a launch name like `Falcon 9 Block 5 | Starlink Group 17-42` into `https://rocketlaunch.org/mission-falcon-9-block-5-starlink-group-17-42`.

**Dark / light mode** uses a `data-theme` attribute on `<html>`. The selected theme is stored in `localStorage` and applied before first paint via an inline script in `<head>` to prevent flash.

---

## Environment Variables

| Variable | File | Description |
|---|---|---|
| `NASA_API` | `data.env` | NASA Open APIs key — free at api.nasa.gov |

---

## Known Limitations

- No server-side caching — heavy API pages (NeoWs, launches) make fresh HTTP requests on every visit
- The Space Devs free tier is rate-limited; the launches page may occasionally return a 429
- ISS position is live but the Open Notify API has no SLA
- No tests
