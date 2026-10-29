#!/usr/bin/env python3
"""GLANCE -- v0.3.0

Daily briefing module for ALIS. Replaces the claude.ai-scheduled morning
brief: assembles a daily summary from existing ALIS data sources (never
re-derives their logic) and serves it over a small JSON API, so it can be
pushed at 6:30am weekdays via alis-push and answered by voice
("Hey ALIS, what's happening today").

v0.3.0 (29 Oct 2026, overnight build):
  - Morning push. A background thread sends one notification per day at
    push_time on push_days (config.json, default 06:30 mon-fri) through
    alis-push's /api/notify with kind "glance" (tap opens Home). The last
    push date is kept in data/push_state.json so restarts never double-push,
    and it won't fire more than 3 hours late (e.g. if the box was down).
  - Weather: current conditions from Home Assistant's weather entity
    (config "weather_entity", else the first weather.* entity found).
  - Plain-English text: GET /api/glance/preview returns the push title/body
    and the spoken version without sending anything.
  - Voice: every 10 minutes the spoken version is published to Home
    Assistant as sensor.glance_briefing (attribute "speech"), so an HA
    sentence automation can answer "what's happening today" without
    calling out to this box.
  - POST /api/push_now (localhost only) sends the push immediately, for
    testing. Never called automatically.
  - Content sections (news, scheduled tasks, Voiceless Druid/ALIS content,
    home-tech projects) arrive via Google Drive: a claude.ai scheduled task
    drops glance_content_cache.json in a shared Drive folder each weekday
    morning and scripts/pull_glance_cache.py (cron 06:12/06:20/06:28)
    downloads it to data/content_cache.json.

v0.2.0: calendar section wired (calendar.waters_family via HA REST API,
household-wide, unfiltered -- Alan's decision 29 Oct 2026).
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

VERSION = "0.3.0"
BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE, "data")
STATE_FILE = os.path.join(DATA_DIR, "glance_state.json")
PUSH_STATE_FILE = os.path.join(DATA_DIR, "push_state.json")
PUSH_LOG = os.path.join(DATA_DIR, "push.log")
CONFIG_FILE = os.path.join(BASE, "config.json")
CALENDAR_ENTITY = "calendar.waters_family"
HA_SENSOR = "sensor.glance_briefing"

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


def log(msg):
    line = time.strftime("%Y-%m-%d %H:%M:%S ") + msg
    print(line, flush=True)
    try:
        with open(PUSH_LOG, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def load_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def save_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=2)
    os.replace(tmp, path)


def load_state():
    return load_json(STATE_FILE)


def save_state(state):
    save_json(STATE_FILE, state)


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


def ha(method, path, body=None, timeout=10):
    """Small Home Assistant REST helper. Returns parsed JSON or {"error": ...}."""
    ha_url = CONFIG.get("ha_url")
    ha_token = CONFIG.get("ha_token")
    if not ha_url or not ha_token:
        return {"error": "ha_url/ha_token not configured"}
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(ha_url + path, data=data, method=method,
                                 headers={"Authorization": "Bearer " + ha_token,
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        return {"error": f"Home Assistant said HTTP {e.code}"}
    except Exception as e:
        return {"error": str(e)}


def get_calendar():
    """Today + tomorrow's events from the Waters Family calendar (household-wide, unfiltered)."""
    now_local = datetime.datetime.now().astimezone()
    start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + datetime.timedelta(days=2)  # today + tomorrow

    params = urllib.parse.urlencode({"start": start.isoformat(), "end": end.isoformat()})
    events = ha("GET", f"/api/calendars/{CALENDAR_ENTITY}?{params}")
    if isinstance(events, dict) and "error" in events:
        return events

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


_weather_entity = {"id": CONFIG.get("weather_entity")}


def get_weather():
    """Current conditions from HA's weather entity (no forecast call -- keeps it cheap)."""
    ent = _weather_entity["id"]
    if not ent:
        states = ha("GET", "/api/states")
        if isinstance(states, dict) and "error" in states:
            return states
        cands = [s["entity_id"] for s in states if s.get("entity_id", "").startswith("weather.")]
        if not cands:
            return {"error": "no weather entity in Home Assistant"}
        # prefer the default "forecast_home"-style entity if there is one
        cands.sort(key=lambda e: (0 if "home" in e else 1, e))
        ent = _weather_entity["id"] = cands[0]
    st = ha("GET", f"/api/states/{ent}")
    if "error" in st:
        return st
    a = st.get("attributes", {})
    return {
        "entity": ent,
        "condition": st.get("state"),
        "temperature": a.get("temperature"),
        "unit": a.get("temperature_unit", "°C"),
        "wind_speed": a.get("wind_speed"),
        "humidity": a.get("humidity"),
    }


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
    tasks, Voiceless Druid/ALIS content, home-tech projects). That task drops the
    file in a shared Google Drive folder; scripts/pull_glance_cache.py (cron)
    downloads it to data/content_cache.json."""
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
    """Assemble the daily briefing."""
    cal = get_calendar()
    brief = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "version": VERSION,
        "sections": {
            "calendar": cal,
            "weather": get_weather(),
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


# ---------------------------------------------------------------- plain-English text

def _hhmm(when):
    """'2026-10-30T12:00:00+00:00' -> '12:00' (local)."""
    try:
        dt = datetime.datetime.fromisoformat(when)
        return dt.astimezone().strftime("%H:%M")
    except Exception:
        return ""


def _events(cal, day):
    if not isinstance(cal, dict):
        return []
    out = []
    for e in cal.get(day, []):
        if "bin" in e["title"].lower():
            continue  # bins get their own line
        t = "" if e.get("all_day") else _hhmm(e.get("when") or "")
        out.append((e["title"], t))
    return out


def _alerts(sec):
    a = sec.get("weather_alerts") or {}
    return [x for x in (a.get("alerts") or []) if isinstance(x, dict)]


CONDITIONS = {"clear-night": ("Clear", "clear"), "cloudy": ("Cloudy", "cloudy"), "exceptional": ("Unusual weather", "unusual weather"),
              "fog": ("Foggy", "foggy"), "hail": ("Hail", "hailing"), "lightning": ("Thunder", "thundery"),
              "lightning-rainy": ("Thunder and rain", "thundery and raining"), "partlycloudy": ("Partly cloudy", "partly cloudy"),
              "pouring": ("Heavy rain", "pouring with rain"), "rainy": ("Rain", "raining"), "snowy": ("Snow", "snowing"),
              "snowy-rainy": ("Sleet", "sleeting"), "sunny": ("Sunny", "sunny"), "windy": ("Windy", "windy"),
              "windy-variant": ("Windy", "windy and cloudy")}


def _cond(c, spoken=False):
    c = str(c or "")
    return CONDITIONS.get(c, (c.replace("-", " ").capitalize(), c.replace("-", " ")))[1 if spoken else 0]


def _ordinal(n):
    return "%d%s" % (n, "th" if 11 <= n % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th"))


def compose(brief):
    """-> dict(title, body, speech) built from a brief. Body fits alis-push's 200-char limit."""
    sec = brief.get("sections", {})
    now = datetime.datetime.now()
    cal = sec.get("calendar")
    wx = sec.get("weather") or {}
    alerts = _alerts(sec)
    bins = (sec.get("bins_ev_aircraft") or {}).get("bins") or []
    vault = sec.get("vault_health") or {}
    today = _events(cal, "today")

    # ---- push (short)
    bits = [now.strftime("%a %-d %b")]
    if wx.get("condition") and wx.get("temperature") is not None:
        bits.append("%s %s%s" % (_cond(wx["condition"]), round(float(wx["temperature"])), wx.get("unit", "°C")))
    if today:
        bits.append("Today: " + ", ".join(("%s %s" % (t, h)).strip() for t, h in today[:3]) + (" +%d more" % (len(today) - 3) if len(today) > 3 else ""))
    else:
        bits.append("Nothing on the calendar today")
    if bins:
        bits.append("Bins: " + ", ".join(("%s (%s)" % (b["title"], b["day"])) for b in bins[:2]))
    if alerts:
        bits.append("⚠ %d warning%s" % (len(alerts), "" if len(alerts) == 1 else "s"))
    if vault.get("level") in ("amber", "red"):
        bits.append("VAULT %s" % vault["level"])
    body = " · ".join(bits)
    if len(body) > 200:
        body = body[:197] + "..."

    # ---- speech (longer, for voice)
    s = ["Good morning." if now.hour < 12 else ("Good afternoon." if now.hour < 18 else "Good evening.")]
    s.append("It's %s the %s of %s." % (now.strftime("%A"), _ordinal(now.day), now.strftime("%B")))
    if wx.get("condition") and wx.get("temperature") is not None:
        s.append("Right now it's %s and %s degrees." % (_cond(wx["condition"], spoken=True), round(float(wx["temperature"]))))
    if alerts:
        s.append("There %s %d weather or emergency warning%s in force: %s." % (
            "is" if len(alerts) == 1 else "are", len(alerts), "" if len(alerts) == 1 else "s",
            "; ".join(str(a.get("title") or a.get("headline") or "untitled")[:80] for a in alerts[:2])))
    if isinstance(cal, dict) and "error" in cal:
        s.append("I couldn't read the calendar.")
    elif today:
        s.append("On the calendar today: " + ", ".join(("%s at %s" % (t, h)) if h else t for t, h in today[:5]) + ".")
    else:
        s.append("There's nothing on the calendar today.")
    if bins:
        s.append("Bins: " + ", ".join("%s %s" % (b["title"], b["day"]) for b in bins[:2]) + ".")
    if vault.get("level") in ("amber", "red"):
        s.append("Heads up: VAULT is showing %s." % vault["level"])
    news = sec.get("ai_news_uk_news") or {}
    if not news.get("stale"):
        uk = [n.get("headline", "") for n in (news.get("uk_news") or [])[:2]]
        ai = [n.get("headline", "") for n in (news.get("ai_advancements") or [])[:1]]
        heads = [h.split(";")[0].split(" — ")[0][:140] for h in uk + ai if h]
        if heads:
            s.append("In the news: " + " Also, ".join(h.rstrip(".") + "." for h in heads))
    speech = " ".join(s)
    return {"title": "Good morning — your GLANCE brief", "body": body, "speech": speech}


# ---------------------------------------------------------------- push + HA publishing

DAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def push_days():
    raw = str(CONFIG.get("push_days", "mon-fri")).lower().replace(" ", "")
    days = set()
    for part in raw.split(","):
        if "-" in part:
            a, b = part.split("-", 1)
            if a in DAY_NAMES and b in DAY_NAMES:
                i, j = DAY_NAMES.index(a), DAY_NAMES.index(b)
                days.update(range(i, j + 1) if i <= j else list(range(i, 7)) + list(range(0, j + 1)))
        elif part in DAY_NAMES:
            days.add(DAY_NAMES.index(part))
    return days or set(range(5))


def send_push(text, tag_suffix=None):
    url = CONFIG.get("alis_push_notify_url", "http://127.0.0.1:8091/api/notify")
    payload = {"title": text["title"], "body": text["body"], "kind": "glance",
               "tag": "alis-glance-" + (tag_suffix or time.strftime("%Y%m%d"))}
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)


def publish_to_ha(text):
    """Put the spoken brief into HA as sensor.glance_briefing so a sentence automation can read it."""
    res = ha("POST", f"/api/states/{HA_SENSOR}", {
        "state": time.strftime("%H:%M"),
        "attributes": {"friendly_name": "GLANCE briefing", "icon": "mdi:newspaper-variant-outline",
                       "speech": text["speech"], "summary": text["body"],
                       "updated": time.strftime("%Y-%m-%dT%H:%M:%S%z")},
    })
    return res


def scheduler():
    hh, mm = (int(x) for x in str(CONFIG.get("push_time", "06:30")).split(":"))
    days = push_days()
    last_ha = 0
    retry_after = 0
    log(f"scheduler up: push {hh:02d}:{mm:02d} on days {sorted(days)}")
    while True:
        try:
            now = datetime.datetime.now()
            # publish spoken text to HA every 10 min (and straight away at start-up)
            if time.time() - last_ha > 600:
                text = compose(build_brief())
                r = publish_to_ha(text)
                last_ha = time.time()
                if isinstance(r, dict) and "error" in r:
                    log("HA publish failed: " + r["error"])

            st = load_json(PUSH_STATE_FILE)
            today = now.date().isoformat()
            due = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            in_window = due <= now < due + datetime.timedelta(hours=3)
            if (now.weekday() in days and in_window and st.get("last_push_date") != today
                    and time.time() >= retry_after):
                text = compose(build_brief())
                try:
                    res = send_push(text)
                    st.update(last_push_date=today, last_push_at=now.isoformat(timespec="seconds"),
                              last_result=res, last_body=text["body"])
                    save_json(PUSH_STATE_FILE, st)
                    log(f"pushed: {res} | {text['body']}")
                except Exception as e:
                    st["attempts_" + today] = st.get("attempts_" + today, 0) + 1
                    save_json(PUSH_STATE_FILE, st)
                    log(f"push failed (attempt {st['attempts_' + today]}): {e}")
                    retry_after = time.time() + 300
                    if st["attempts_" + today] >= 6:
                        st["last_push_date"] = today  # give up for today
                        save_json(PUSH_STATE_FILE, st)
                        log("giving up on today's push")
        except Exception as e:
            log(f"scheduler error: {e}")
        time.sleep(20)


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
        if path == "/api/glance/preview":
            text = compose(build_brief())
            text["push_state"] = load_json(PUSH_STATE_FILE)
            return self._json(200, text)
        return self._json(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?")[0]
        if path == "/api/push_now":
            if self.client_address[0] not in ("127.0.0.1", "::1"):
                return self._json(403, {"error": "forbidden"})
            text = compose(build_brief())
            try:
                res = send_push(text, tag_suffix="test-" + time.strftime("%H%M%S"))
            except Exception as e:
                return self._json(502, {"error": str(e)})
            log(f"manual push: {res}")
            return self._json(200, {"ok": True, "result": res, "sent": text})
        return self._json(404, {"error": "not found"})


if __name__ == "__main__":
    threading.Thread(target=scheduler, daemon=True).start()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"GLANCE server on http://0.0.0.0:{PORT} (v{VERSION})", flush=True)
    srv.serve_forever()
