#!/usr/bin/env python3
"""GLANCE -- v0.1.0

Daily briefing module for ALIS. Replaces the claude.ai-scheduled morning
brief: assembles a daily summary from existing ALIS data sources (never
re-derives their logic) and serves it over a small JSON API, so it can be
pushed at 6:30am weekdays via alis-push and answered by voice
("Hey ALIS, what's happening today").

Stage 3 (this file, first cut): server skeleton only. GET /api/glance
returns a placeholder brief shape -- each real content source (calendar,
weather/alerts, VAULT health, bins/EV/aircraft, Hive/Zigbee, damp
placeholder, news/AI/Voiceless-Druid/scheduled-tasks/home-tech) gets wired
in one at a time next, confirming each before moving to the next.
"""
import json
import os
import threading
import time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

VERSION = "0.1.0"
BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE, "data")
STATE_FILE = os.path.join(DATA_DIR, "glance_state.json")
CONFIG_FILE = os.path.join(BASE, "config.json")

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


def build_brief():
    """Assemble the daily briefing. Placeholder shape for Stage 3 --
    sections get filled in one at a time in Stage 4."""
    brief = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "version": VERSION,
        "sections": {
            "calendar": None,
            "weather_alerts": None,
            "vault_health": None,
            "bins_ev_aircraft": None,
            "hive_zigbee": None,
            "office_damp": {"status": "not yet monitoring"},
            "ai_news_uk_news": None,
            "voiceless_druid_alis_content": None,
            "scheduled_tasks": None,
            "home_tech_projects": None,
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
