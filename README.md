# Horizon Intelligence

A space-themed live data dashboard built with Flask. Aggregates real-time data from several public space APIs into a clean, readable interface with dark and light mode support.

---

## Pages


| Page                      | What it shows                                                                                 |
| --------------------------- | ----------------------------------------------------------------------------------------------- |
| **Home**                  | NASA Astronomy Picture of the Day + two random Earth images from NASA EPIC                    |
| **ISS Tracker**           | Live International Space Station position on an interactive D3 globe, updated every 5 seconds |
| **Aurora Watch**          | UK view: real-time geomagnetic activity across UK monitoring sites (AuroraWatch UK). World view: NOAA OVATION aurora forecast on a D3 globe with the best place to see it |
| **Sky Alerts**            | Sign-up for ISS pass alerts (~30 min before) and UK aurora alerts by email or Telegram, checked against local weather |
| **Asteroid Surveillance** | The 10 nearest asteroid close approaches in the next 7 days from NASA NeoWs                   |
| **Launch Operations**     | Next 5 upcoming rocket launches with mission descriptions, linked to RocketLaunch.org         |

---

## Tech Stack

- **Backend** — Python 3, Flask, Requests, python-dotenv, pytz
- **Templating** — Jinja2 (`base.html` → page templates)
- **Frontend** — Vanilla JS, D3.js v7, TopoJSON v3
- **Styling** — Plain CSS with custom properties, dark/light mode via `data-theme` attribute

- **Alerts** — SQLite (subscribers), `sgp4` (ISS orbit), Gmail SMTP, Telegram Bot API, GitHub Actions (scheduler)

No task queue, no frontend framework.

---

## External APIs


| API                                                        | Used for                                     | Auth              |
| ------------------------------------------------------------ | ---------------------------------------------- | ------------------- |
| [NASA APOD](https://api.nasa.gov/)                         | Astronomy Picture of the Day                 | API key           |
| [NASA EPIC](https://epic.gsfc.nasa.gov/)                   | Earth Polychromatic Imaging Camera photos    | API key           |
| [NASA NeoWs](https://api.nasa.gov/)                        | Near-Earth asteroid close approaches         | API key           |
| [The Space Devs](https://thespacedevs.com/)                | Upcoming rocket launches                     | None (public)     |
| [AuroraWatch UK](https://aurorawatch.lancs.ac.uk/)         | Geomagnetic activity at UK sites             | None (public XML) |
| [Open Notify](http://api.open-notify.org/)                 | ISS real-time position (polled from browser) | None              |
| [world-atlas](https://cdn.jsdelivr.net/npm/world-atlas@2/) | Country boundaries TopoJSON for maps         | None (CDN)        |

---

## Setup

### 1. Clone and create a virtual environment

```bash
git clone https://github.com/LegradiK/all_about_space_web_app.git
cd all_about_space_web_app
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
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
├── alerts.py             # Sky Alerts blueprint: sign-up, confirm, unsubscribe, alert check
├── iss_passes.py         # Predicts visible ISS passes (sgp4 + sun position + Earth shadow)
├── mailer.py             # Sends email over SMTP (Gmail)
├── telegram_bot.py       # Sends Telegram messages from the bot
├── telegram_setup.py     # One-off: points the bot's webhook at this site
├── alerts.db             # Subscribers, created automatically (gitignored)
├── data.env              # API keys and secrets (gitignored)
├── .github/workflows/
│   └── sky-alerts.yml    # Calls /tasks/check-alerts every 10 minutes
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
    ├── launches.html     # Upcoming launch list
    ├── alerts.html       # Sky Alerts sign-up page
    ├── alerts_telegram.html  # "Open Telegram" step after choosing Telegram
    ├── emails/           # Confirmation, ISS and aurora email templates (HTML + text)
    └── telegram/         # Telegram alert and bot reply messages
```

---

## Architecture Notes

**All API calls are synchronous and happen on each page load** — there is no caching or background refresh on the server side. If an external API is slow or down, that page will be slow or show a fallback message.

**The ISS position is fetched entirely in the browser** — `iss.html` polls `open-notify.org` every 5 seconds via `fetch()` and animates the D3 globe accordingly. The server is not involved after the initial page render.

**Aurora data goes through a Flask proxy** — `GET /api/aurora-status` forwards to the AuroraWatch XML endpoint to avoid browser CORS restrictions. The frontend then parses the XML itself.

**Launch URLs are slugified to RocketLaunch.org** — the `rocketlaunch_url()` function in `main.py` converts a launch name like `Falcon 9 Block 5 | Starlink Group 17-42` into `https://rocketlaunch.org/mission-falcon-9-block-5-starlink-group-17-42`.

**Dark / light mode** uses a `data-theme` attribute on `<html>`. The selected theme is stored in `localStorage` and applied before first paint via an inline script in `<head>` to prevent flash.

---

## Sky Alerts

Visitors sign up on `/alerts` with their closest city (from `static/cities.json`) and choose **email** (confirmed by a link) or **Telegram** (confirmed by pressing Start in the bot, which sends `/start <token>` to the webhook). In Telegram, `/status` shows their settings and `/stop` turns alerts off; anyone who blocks the bot is removed automatically.
Every 10 minutes the GitHub Actions workflow `sky-alerts.yml` POSTs to `/tasks/check-alerts`, which:

- **ISS** — predicts passes from the CelesTrak orbit and emails when a pass starts in 15–40 minutes, climbs ≥ 25°, is magnitude −1 or brighter, is sunlit while the city is dark, and Open-Meteo forecasts ≤ 40% cloud.
- **Aurora** — emails when AuroraWatch UK is Red (whole UK), Amber (north of ~53.3°N) or Yellow (north of 57°N), the sun is 12°+ below the horizon and cloud cover is ≤ 50%. At most one email per night unless the level rises.

The 594 places in `static/cities.json` are grouped into 19 regions (north to south), each with coordinates from [GeoNames](https://www.geonames.org/) (CC BY 4.0). The sign-up page filters them as you type, groups matches by region, and says clearly when a place isn't on the list. Typing a region name such as "Lake District" shows all its places.

**Telegram on PythonAnywhere's free tier.** The free tier can't reach `api.telegram.org`, so nothing Telegram-related needs it:
- bot replies (`/start`, `/status`, `/stop`, `/help`) are returned in the webhook's HTTP response, which Telegram delivers itself;
- Telegram alerts are queued and returned by `/tasks/check-alerts`; the GitHub Action sends them and reports blocked chats back to `/tasks/telegram-gone`. Set `TELEGRAM_DELIVERY=github` to skip trying to send directly (`auto`, the default, tries first and queues on failure);
- run `telegram_setup.py` on your own computer (with `SITE_URL` pointing at the live site), not on PythonAnywhere.

### Setting up alerts

1. **Gmail app password** — Google Account → Security → 2-Step Verification → App passwords. Put it in `data.env` as `MAIL_PASSWORD`, with your address as `MAIL_USERNAME`.
2. **Token** — generate one with `python -c "import secrets; print(secrets.token_urlsafe(32))"` and put it in `data.env` as `ALERT_TASK_TOKEN`. Add `SITE_URL` too.
3. **GitHub secrets** — repo Settings → Secrets and variables → Actions: add `SITE_URL`, `ALERT_TASK_TOKEN` and, for Telegram, `TELEGRAM_BOT_TOKEN` (same values as `data.env`).
4. **Telegram bot** — in Telegram, message [@BotFather](https://t.me/BotFather), send `/newbot` and pick a name and a username ending in `bot`. Put the token in `data.env` as `TELEGRAM_BOT_TOKEN`, the username (no @) as `TELEGRAM_BOT_USERNAME`, and a random string as `TELEGRAM_WEBHOOK_SECRET`. The Telegram option only appears on the form once these are set.
5. **PythonAnywhere** — install the requirements in the web app's virtualenv, upload, reload the web app. Then, on your own computer, run `python telegram_setup.py` once to connect the bot to `/telegram/webhook`. Run the GitHub workflow once by hand from the Actions tab and check the JSON it prints.

Without `MAIL_USERNAME` set, emails are printed to the console instead of sent, which is handy locally.

---

## Environment Variables


| Variable   | File       | Description                                |
| ------------ | ------------ | -------------------------------------------- |
| `NASA_API` | `data.env` | NASA Open APIs key — free at api.nasa.gov |
| `MAIL_USERNAME` | `data.env` | Gmail address the alerts are sent from |
| `MAIL_PASSWORD` | `data.env` | Gmail app password (not your normal password) |
| `MAIL_FROM` | `data.env` | Optional sender name, e.g. `Horizon Intelligence <you@gmail.com>` |
| `SITE_URL` | `data.env` + GitHub secret | Public site address, e.g. `https://legradik.pythonanywhere.com` |
| `ALERT_TASK_TOKEN` | `data.env` + GitHub secret | Long random string that protects `/tasks/check-alerts` |
| `TELEGRAM_BOT_TOKEN` | `data.env` | Bot token from @BotFather |
| `TELEGRAM_BOT_USERNAME` | `data.env` | Bot username without the @, used for the "Open Telegram" link |
| `TELEGRAM_WEBHOOK_SECRET` | `data.env` | Random string Telegram sends back on every webhook call |
| `TELEGRAM_DELIVERY` | `data.env` | `github` on PythonAnywhere free tier (alerts sent by the GitHub Action); default `auto` |

---

## Known Limitations

- No server-side caching — heavy API pages (NeoWs, launches) make fresh HTTP requests on every visit
- The Space Devs free tier is rate-limited; the launches page may occasionally return a 429
- ISS position is live but the Open Notify API has no SLA
- GitHub pauses scheduled workflows after 60 days without repo activity; re-enable it from the Actions tab
- Gmail limits sending to about 500 emails a day
- No tests
