## Running the app

```bash
source venv/bin/activate
python main.py
```

The dev server starts at `http://127.0.0.1:5000` with debug mode on. There are no tests and no linter configured.

## Environment

The NASA API key is loaded from `data.env` (not `.env`) via `python-dotenv`. The variable is `NASA_API`. This file is gitignored.

To install dependencies into the venv:
```bash
source venv/bin/activate
pip install flask requests python-dotenv pytz
```

## Architecture

Single-page Flask app (`main.py`) with one user-facing route (`/`) and one proxy route (`/api/aurora-status`).

**Data flow**: on every `GET /`, five external APIs are called synchronously and their results are passed directly to `home.html`. There is no caching, database, or background task runner.

| Function | API | Notes |
|---|---|---|
| `get_apod()` | NASA APOD | Astronomy Picture of the Day; supports image and video media types |
| `get_epic()` | NASA EPIC | Picks 1 random Earth image; constructs the image URL from the archive path |
| `get_neows()` | NASA NeoWs | Next 7 days of asteroid close-approaches; returns 5 sorted by date |
| `get_launches()` | The Space Devs (`ll.thespacedevs.com/2.3.0`) | Next 4 upcoming rocket launches |
| `get_iss()` | `open-notify.org` | ISS lat/lon; only used server-side to pass initial coords |
| `aurora_proxy()` | AuroraWatch UK XML | CORS proxy — frontend fetches `/api/aurora-status`, Flask forwards to Lancaster University |

**Templates** use Jinja2 inheritance: `base.html` → `home.html` (extends) + `footer.html` (included). `sidebar.html` exists but is currently empty.

**Frontend** (all inline in `home.html`):
- ISS globe: D3 orthographic projection, polls `open-notify.org` every 5 seconds directly from the browser, animates rotation to track the ISS
- Aurora map: D3 Mercator projection of UK, parses AuroraWatch XML from the proxy, refreshes every 3 minutes
- Both visualisations load world TopoJSON from `cdn.jsdelivr.net/npm/world-atlas@2`

**CSS** (`static/style.css`): dark space theme using CSS custom properties (`--bg-primary`, `--accent`, etc.). Layout is `hero` (2-column grid: APOD + ISS/EPIC sidebar) above a `dashboard-grid` (auto-fit, 3 cards: launches, asteroids, aurora).
