"""Predict visible ISS passes for a place on Earth.

Uses the ISS orbit (TLE from CelesTrak) with the small `sgp4` library.
A pass counts as *visible* when, at the same moment:
  - the ISS is at least MIN_ELEVATION degrees above the horizon,
  - the ISS is in sunlight (not in Earth's shadow), and
  - the observer's sky is dark (sun more than 6 degrees below the horizon).
"""
import math
from datetime import datetime, timedelta, timezone

import requests
from sgp4.api import Satrec, jday

TLE_URL = 'https://celestrak.org/NORAD/elements/gp.php?CATNR=25544&FORMAT=TLE'
TLE_CACHE_HOURS = 12

EARTH_RADIUS_KM = 6378.137
EARTH_FLATTENING = 1 / 298.257223563
MIN_ELEVATION = 10        # degrees
DARK_SUN_ALTITUDE = -6    # degrees: end of civil twilight
ISS_STD_MAGNITUDE = -1.8  # brightness at 1000 km, half lit

_tle_cache = {'lines': None, 'fetched_at': None}


def get_tle():
    """Latest ISS TLE, cached for 12 hours. Keeps the old one if CelesTrak is down."""
    now = datetime.now(timezone.utc)
    fetched_at = _tle_cache['fetched_at']
    if fetched_at and now - fetched_at < timedelta(hours=TLE_CACHE_HOURS):
        return _tle_cache['lines']
    try:
        response = requests.get(TLE_URL, timeout=15)
        response.raise_for_status()
        lines = [l.strip() for l in response.text.strip().splitlines()]
        line1 = next(l for l in lines if l.startswith('1 '))
        line2 = next(l for l in lines if l.startswith('2 '))
        _tle_cache['lines'] = (line1, line2)
        _tle_cache['fetched_at'] = now
    except Exception as e:
        print("ISS TLE fetch failed:", e)
    return _tle_cache['lines']


# ───────────── vector helpers ─────────────

def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _norm(a):
    return math.sqrt(_dot(a, a))


def _sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


# ───────────── time, sun and observer ─────────────

def _days_since_j2000(when):
    return (when - datetime(2000, 1, 1, 12, tzinfo=timezone.utc)).total_seconds() / 86400


def _gmst_rad(when):
    """Greenwich sidereal angle: how far Earth has turned."""
    d = _days_since_j2000(when)
    return math.radians((280.46061837 + 360.98564736629 * d) % 360)


def _sun_direction(when):
    """Unit vector towards the sun in the inertial (equatorial) frame. Accurate to ~0.01°."""
    d = _days_since_j2000(when)
    g = math.radians(357.529 + 0.98560028 * d)
    q = 280.459 + 0.98564736 * d
    lam = math.radians(q + 1.915 * math.sin(g) + 0.020 * math.sin(2 * g))
    eps = math.radians(23.439 - 0.00000036 * d)
    return (math.cos(lam), math.cos(eps) * math.sin(lam), math.sin(eps) * math.sin(lam))


def _observer_ecef(lat, lon, height_km=0.0):
    """Observer position in Earth-fixed coordinates (WGS84)."""
    la, lo = math.radians(lat), math.radians(lon)
    e2 = EARTH_FLATTENING * (2 - EARTH_FLATTENING)
    n = EARTH_RADIUS_KM / math.sqrt(1 - e2 * math.sin(la) ** 2)
    return ((n + height_km) * math.cos(la) * math.cos(lo),
            (n + height_km) * math.cos(la) * math.sin(lo),
            (n * (1 - e2) + height_km) * math.sin(la))


def _inertial_to_ecef(v, gmst):
    c, s = math.cos(gmst), math.sin(gmst)
    return (c * v[0] + s * v[1], -s * v[0] + c * v[1], v[2])


def _alt_az(lat, lon, obs_ecef, target_ecef):
    """Elevation and azimuth (degrees) of a target seen from the observer, plus range in km."""
    la, lo = math.radians(lat), math.radians(lon)
    rx, ry, rz = _sub(target_ecef, obs_ecef)
    east = -math.sin(lo) * rx + math.cos(lo) * ry
    north = (-math.sin(la) * math.cos(lo) * rx - math.sin(la) * math.sin(lo) * ry
             + math.cos(la) * rz)
    up = (math.cos(la) * math.cos(lo) * rx + math.cos(la) * math.sin(lo) * ry
          + math.sin(la) * rz)
    rng = math.sqrt(rx * rx + ry * ry + rz * rz)
    elevation = math.degrees(math.asin(up / rng))
    azimuth = math.degrees(math.atan2(east, north)) % 360
    return elevation, azimuth, rng


def sun_altitude(lat, lon, when):
    """Sun's altitude in degrees at a place and time (negative = below the horizon)."""
    sun = _inertial_to_ecef(_sun_direction(when), _gmst_rad(when))
    far_sun = tuple(c * 1.496e8 for c in sun)
    elevation, _, _ = _alt_az(lat, lon, _observer_ecef(lat, lon), far_sun)
    return elevation


def compass(azimuth):
    points = ['N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW']
    return points[int((azimuth + 22.5) // 45) % 8]


# ───────────── pass prediction ─────────────

def _iss_sunlit(sat_inertial, sun_dir):
    """Cylindrical Earth-shadow model: in shadow only if behind Earth and inside its radius."""
    along = _dot(sat_inertial, sun_dir)
    if along >= 0:
        return True
    perpendicular = _sub(sat_inertial, tuple(along * c for c in sun_dir))
    return _norm(perpendicular) > EARTH_RADIUS_KM


def _magnitude(range_km, sat_inertial, obs_inertial, sun_dir):
    """Rough visual magnitude (lower = brighter). Diffuse-sphere phase model."""
    to_obs = _sub(obs_inertial, sat_inertial)
    cos_phase = _dot(sun_dir, to_obs) / _norm(to_obs)
    phase = math.acos(max(-1.0, min(1.0, cos_phase)))  # 0 = fully lit, as seen by observer
    lit_fraction = (math.sin(phase) + (math.pi - phase) * math.cos(phase)) / math.pi
    lit_fraction = max(lit_fraction, 1e-3)
    return ISS_STD_MAGNITUDE + 5 * math.log10(range_km / 1000) - 2.5 * math.log10(lit_fraction / 0.5)


def visible_passes(lat, lon, start=None, hours=1.0, step_seconds=10, tle=None):
    """List of visible passes starting in the next `hours`.

    Each pass: start/end (UTC datetimes of the visible part), max_elevation,
    start/end direction, and brightest magnitude.
    """
    tle = tle or get_tle()
    if not tle:
        return []
    sat = Satrec.twoline2rv(*tle)
    start = start or datetime.now(timezone.utc)
    obs_ecef = _observer_ecef(lat, lon)

    passes, current = [], None
    # Look a little past the window so a pass that starts near the end is complete
    steps = int((hours * 3600 + 20 * 60) / step_seconds)
    for i in range(steps + 1):
        when = start + timedelta(seconds=i * step_seconds)
        jd, fr = jday(when.year, when.month, when.day, when.hour, when.minute,
                      when.second + when.microsecond / 1e6)
        err, sat_inertial, _ = sat.sgp4(jd, fr)
        if err != 0:
            continue
        gmst = _gmst_rad(when)
        sun_dir = _sun_direction(when)
        elevation, azimuth, rng = _alt_az(lat, lon, obs_ecef, _inertial_to_ecef(sat_inertial, gmst))

        visible = (elevation >= MIN_ELEVATION
                   and _iss_sunlit(sat_inertial, sun_dir)
                   and sun_altitude(lat, lon, when) <= DARK_SUN_ALTITUDE)

        if visible:
            # observer position back in the inertial frame, for the brightness estimate
            c, s = math.cos(gmst), math.sin(gmst)
            obs_inertial = (c * obs_ecef[0] - s * obs_ecef[1], s * obs_ecef[0] + c * obs_ecef[1], obs_ecef[2])
            mag = _magnitude(rng, sat_inertial, obs_inertial, sun_dir)
            if current is None:
                current = {'start': when, 'start_az': azimuth, 'max_elevation': elevation,
                           'max_time': when, 'magnitude': mag}
            current['end'], current['end_az'] = when, azimuth
            if elevation > current['max_elevation']:
                current['max_elevation'], current['max_time'] = elevation, when
            current['magnitude'] = min(current['magnitude'], mag)
        elif current is not None:
            passes.append(current)
            current = None
    if current is not None:
        passes.append(current)

    window_end = start + timedelta(hours=hours)
    result = []
    for p in passes:
        if p['start'] > window_end:
            continue
        p['duration_min'] = round((p['end'] - p['start']).total_seconds() / 60, 1)
        p['start_dir'] = compass(p['start_az'])
        p['end_dir'] = compass(p['end_az'])
        result.append(p)
    return result
