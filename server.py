#!/usr/bin/env python3
"""GLANCE -- v0.2.0

Daily briefing module for ALIS. Replaces the claude.ai-scheduled morning
brief: assembles a daily summary from existing ALIS data sources (never
re-derives their logic) and serves it over a small JSON API, so it can be
pushed at 6:30am weekdays via alis-push and answered by voice
("Hey ALIS, what's happening today").

v0.2.0: calendar section wired. Pulls today + tomorrow's events from the
Waters Family calendar (calendar.waters_family in Home Assistant) via HA's
REST API. Household-wide for now, no "whose entry is it" filtering --
Alan's decision (29 Oct 2026): show everything, revisit personalising to
just his own entries later.

Still placeholder: weather_alerts, vault_health, bins_ev_aircraft,
hive_zigbee, ai_news_uk_news, voiceless_druid_alis_content,
scheduled_tasks, home_tech_projects.
"""
import json
import os
import threading
import time
import datetime
import urllib.request
import urllib.error
import urllib.parse
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

VERSION = "0.2.0"
BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE, "data")
STATE_FILE = os.path.join(DATA_DIR, "glance_state.json")
CONFIG_FILE = os.path.join(BASE, "config.json")
CALENDAR_ENTITY = "calendar.waters_family"

os.makedirs(DATA_DIR, exist_ok=True)


def load_config():
    try:
        with open(CONFIG_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


CONFIG = load_config()
PORT = int(os.environ.get("ALIS_GLANCE_PORT", CONFIG.get("port", 3512)))

lock = threading.Lock()


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_FILE)


def get_passthrough(path):
    """GET a JSON endpoint from alis-live (same box, localhost) and return it as-is."""
    base = CONFIG.get("alis_live_base", "http://127.0.0.1:8080")
    req = urllib.request.Request(base + path)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        return {"error": f"alis-live said HTTP {e.code}"}
    except Exception as e:
        return {"error": str(e)}


def get_calendar():
    """Today + tomorrow's events from the Waters Family calendar (household-wide, unfiltered)."""
    ha_url = CONFIG.get("ha_url")
    ha_token = CONFIG.get("ha_token")
    if not ha_url or not ha_token:
        return {"error": "ha_url/ha_token not configured"}

    now_local = datetime.datetime.now().astimezone()
    start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + datetime.timedelta(days=2)  # today + tomorrow

    params = urllib.parse.urlencode({"start": start.isoformat(), "end": end.isoformat()})
    url = f"{ha_url}/api/calendars/{CALENDAR_ENTITY}?{params}"
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + ha_token})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            events = json.load(r)
    except urllib.error.HTTPError as e:
        return {"error": f"Home Assistant said HTTP {e.code}"}
    except Exception as e:
        return {"error": str(e)}

    today_str = start.date().isoformat()
    tomorrow_str = (start.date() + datetime.timedelta(days=1)).isoformat()
    out = {"today": [], "tomorrow": []}
    for e in events:
        summary = e.get("summary", "(untitled)")
        when = e.get("start", {})
        date_part = when.get("dateTime", when.get("date", ""))[:10]
        entry = {"title": summary, "when": when.get("dateTime") or when.get("date"),
                  "all_day": "date" in when}
        if date_part == today_str:
            out["today"].append(entry)
        elif date_part == tomorrow_str:
            out["tomorrow"].append(entry)
    out["today"].sort(key=lambda x: x["when"] or "")
    out["tomorrow"].sort(key=lambda x: x["when"] or "")
    return out


def get_bins_ev_aircraft(cal):
    """Bins: pull from the calendar entries already fetched (titles containing 'bin').
    EV: trimmed summary from alis-live's /api/ev. Aircraft: military/emergency only, from /api/flights."""
    out = {}

    bins = []
    if isinstance(cal, dict):
        for day_key in ("today", "tomorrow"):
            for e in cal.get(day_key, []):
                if "bin" in e["title"].lower():
                    bins.append({"day": day_key, "title": e["title"]})
    out["bins"] = bins

    ev = get_passthrough("/api/ev")
    if "error" in ev:
        out["ev"] = ev
    else:
        out["ev"] = {
            "charger_state": ev.get("charger", {}).get("state"),
            "today_kwh": ev.get("today"),
            "month_kwh": ev.get("month", {}).get("kwh"),
            "month_cost": ev.get("month", {}).get("cost"),
        }

    flights = get_passthrough("/api/flights")
    if "error" in flights:
        out["aircraft"] = flights
    else:
        nearby_mil = [p for p in flights.get("planes", []) if p.get("mil")]
        out["aircraft"] = {"military_nearby": nearby_mil, "emergency": flights.get("emergency", [])}

    return out


CONTENT_CACHE_FILE = os.path.join(DATA_DIR, "content_cache.json")
CONTENT_STALE_AFTER = 36 * 3600  # hours -- covers weekends (task runs weekdays only)


def get_cached_content():
    """Sections filled by the daily content-gathering Claude task (news, scheduled
    tasks, Voiceless Druid/ALIS content, home-tech projects) -- GLANCE's own server
    can't do web search or read claude.ai memory, so a separate scheduled task writes
    data/content_cache.json directly on this box each morning before the push."""
    try:
        with open(CONTENT_CACHE_FILE) as f:
            cache = json.load(f)
    except Exception:
        return {
            "ai_news_uk_news": {"stale": True, "reason": "no content cache yet"},
            "voiceless_druid_alis_content": {"stale": True, "reason": "no content cache yet"},
            "scheduled_tasks": {"stale": True, "reason": "no content cache yet"},
            "home_tech_projects": {"stale": True, "reason": "no content cache yet"},
        }

    generated_at = cache.get("generated_at")
    stale = True
    if generated_at:
        try:
            age = time.time() - time.mktime(time.strptime(generated_at[:19], "%Y-%m-%dT%H:%M:%S"))
            stale = age > CONTENT_STALE_AFTER
        except Exception:
            stale = True

    out = {}
    for key in ("ai_news_uk_news", "voiceless_druid_alis_content", "scheduled_tasks", "home_tech_projects"):
        val = cache.get(key)
        if val is None:
            out[key] = {"stale": True, "reason": "missing from cache"}
        elif stale:
            out[key] = dict(val, stale=True, cache_generated_at=generated_at) if isinstance(val, dict) else {"stale": True, "value": val}
        else:
            out[key] = val
    return out


def build_brief():
    """Assemble the daily briefing. Sections get filled in one at a time."""
    cal = get_calendar()
    brief = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "version": VERSION,
        "sections": {
            "calendar": cal,
            "weather_alerts": get_passthrough("/api/alerts"),
            "vault_health": get_passthrough("/api/vault"),
            "bins_ev_aircraft": get_bins_ev_aircraft(cal),
            "hive_zigbee": {"status": CONFIG.get("hive_zigbee_status", "not set")},
            "office_damp": {"status": "not yet monitoring"},
            **get_cached_content(),
        },
    }
    with lock:
        save_state(brief)
    return brief


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        raw = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/api/health":
            return self._json(200, {"ok": True, "version": VERSION})
        if path == "/api/glance":
            return self._json(200, build_brief())
        return self._json(404, {"error": "not found"})


if __name__ == "__main__":
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"GLANCE server on http://0.0.0.0:{PORT} (v{VERSION})")
    srv.serve_forever()
