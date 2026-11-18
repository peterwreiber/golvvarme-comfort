"""Golvvärme Comfort: price-aware control of an electric floor heating thermostat via Home Assistant."""

import json
import logging
import math
import os
import signal
import threading
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from planner import (DEFAULTS, PRESENCE_DOMAINS, PlanError, build_plan, effective, learn_rates, next_change, presence_home,
                     price_at, vacation_bounds, validate)
from prices import LABELS as PRICE_LABELS, PriceError, load_prices
from community import Community

LOG = logging.getLogger("golvvarme_comfort")
STATIC = Path(__file__).parent / "static"
VERSION = os.environ.get("GOLVVARME_VERSION", "dev")
COMMUNITY = Community("https://raw.githubusercontent.com/peterwreiber/golvvarme-comfort/stats/anvandare.json")
# A cloud thermostat: never write more often than this, except when handing control back.
MIN_WRITE_SECONDS = 300
# After this many writes that the thermostat did not take, retry only every UNCONFIRMED_RETRY_SECONDS.
UNCONFIRMED_LIMIT = 3
UNCONFIRMED_RETRY_SECONDS = 1800
PRICE_CACHE_SECONDS = 900
MAX_BODY = 16384
# Heat-up and cool-down rates are measured again from this much thermostat history this often.
RATE_HISTORY_DAYS = 10
RATE_REFRESH_SECONDS = 6 * 3600
RATE_RETRY_SECONDS = 1800


class HaError(Exception):
    pass


class HomeAssistant:
    def __init__(self, token, url="http://supervisor/core/api"):
        self.token, self.url = token, url.rstrip("/")

    def request(self, path, data=None, timeout=20):
        body = None if data is None else json.dumps(data).encode()
        request = Request(self.url + path, data=body, method="GET" if data is None else "POST", headers={
            "Authorization": f"Bearer {self.token}", "Content-Type": "application/json"})
        try:
            with urlopen(request, timeout=timeout) as response:
                raw = response.read()
        except HTTPError as exc:
            raise HaError(f"Home Assistant svarade {exc.code} på {path.split('?')[0]}.") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise HaError("Kunde inte nå Home Assistant.") from exc
        return json.loads(raw) if raw else None

    def state(self, entity_id):
        return self.request(f"/states/{quote(entity_id)}")

    def states(self):
        return self.request("/states")

    def service(self, domain, name, data, response=False):
        return self.request(f"/services/{domain}/{name}" + ("?return_response" if response else ""), data)

    def time_zone(self):
        return self.request("/config").get("time_zone", "Europe/Stockholm")

    def history(self, entity_id, start, end):
        stamp = lambda t: quote(datetime.fromtimestamp(t).astimezone().isoformat())
        rows = self.request(f"/history/period/{stamp(start)}?filter_entity_id={quote(entity_id)}&end_time={stamp(end)}")
        return rows[0] if rows else []


def degrees(value):
    """Swedish decimal comma, like the rest of the page."""
    return f"{value:.1f}".replace(".", ",")


def parse_time(text):
    return datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp()


def number_or_none(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def thermostat_caps(attrs):
    """What the climate entity supports, from its attributes (works for any Home Assistant thermostat)."""
    modes = attrs.get("hvac_modes") or []
    step = number_or_none(attrs.get("target_temp_step"))
    return {"heat": "heat" in modes, "auto": "auto" in modes, "step": step if step and step > 0 else 0.5,
            "min": number_or_none(attrs.get("min_temp")), "max": number_or_none(attrs.get("max_temp"))}


def setpoint(caps, value):
    """value rounded to the thermostat's step and kept within its limits."""
    value = round(math.floor(value / caps["step"] + 0.5 + 1e-9) * caps["step"], 2)
    if caps["min"] is not None:
        value = max(value, caps["min"])
    if caps["max"] is not None:
        value = min(value, caps["max"])
    return value


def heating_summary(rows, prices, power_kw, start, end):
    """Hours with hvac_action heating in [start, end) and their estimated cost at power_kw.

    price_shift is what the priced kWh would have cost at the period's time-weighted average
    price minus what they cost: positive when the floor heated in the cheaper hours. It is a
    gain from timing, not from using less, and None when prices are missing.
    """
    hours = cost = priced_hours = 0.0
    timeline = []
    for row in rows:
        try:
            timeline.append((parse_time(row["last_changed"]), row.get("attributes", {}).get("hvac_action") == "heating"))
        except (KeyError, ValueError):
            continue
    timeline.append((end, False))
    for (t, heating), (nxt, _) in zip(timeline, timeline[1:]):
        a, b = max(t, start), min(nxt, end)
        if not heating or b <= a:
            continue
        hours += (b - a) / 3600
        while a < b:
            step = min(b, a - a % 900 + 900)
            price = price_at(prices, a)
            if price is not None:
                cost += price * power_kw * (step - a) / 3600
                priced_hours += (step - a) / 3600
            a = step
    weighted = seconds = 0.0
    a = start
    while a < end:
        step = min(end, a - a % 900 + 900)
        price = price_at(prices, a)
        if price is not None:
            weighted += price * (step - a)
            seconds += step - a
        a = step
    average = weighted / seconds if seconds else None
    priced_kwh = priced_hours * power_kw
    shift = None
    if average is not None and priced_kwh > 0:
        shift = {"average_price": round(average, 3), "actual_price": round(cost / priced_kwh, 3),
                 "kwh": round(priced_kwh, 2), "average_cost": round(priced_kwh * average, 2),
                 "difference": round(priced_kwh * average - cost, 2),
                 "complete": math.isclose(priced_hours, hours, abs_tol=0.01) and math.isclose(seconds, end - start, abs_tol=1)}
    return {"hours": round(hours, 2), "kwh": round(hours * power_kw, 2), "cost": round(cost, 2), "price_shift": shift}


class EventLog:
    def __init__(self, path, limit=300):
        self.path, self.limit = path, limit
        self.lock = threading.Lock()
        try:
            self.rows = json.loads(path.read_text())
        except (OSError, ValueError):
            self.rows = []

    def add(self, kind, message):
        with self.lock:
            self.rows = [*self.rows, {"t": time.time(), "kind": kind, "message": message}][-self.limit:]
            try:
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text(json.dumps(self.rows, ensure_ascii=False))
                tmp.replace(self.path)
            except OSError:
                LOG.exception("Kunde inte spara händelseloggen")

    def recent(self, count=100):
        with self.lock:
            return self.rows[-count:][::-1]


class Controller:
    def __init__(self, ha, data_dir):
        self.ha, self.data_dir = ha, Path(data_dir)
        self.lock = threading.RLock()
        self.settings_path = self.data_dir / "settings.json"
        self.events = EventLog(self.data_dir / "events.json")
        self.config = self.load_settings()
        self.tz = None
        self.prices, self.prices_at, self.price_source = [], 0, None
        self.last_write = 0
        # Requested setpoint -> what the thermostat actually stored (devices that ignore our step).
        self.equivalent = {}
        self.last_request = None
        # (target, attempts) for writes the thermostat has not confirmed yet.
        self.unconfirmed = None
        self.stuck_reported = False
        self.committed = {}
        self.status = {"message": "Startar…"}
        self.plan = None
        self.summary_cache = (0, None)
        self.learned, self.learned_at, self.learned_for = None, 0, None
        self.away_since = None

    def refresh_rates(self, now):
        """Measure heat-up and cool-down from the thermostat's history; outside the lock, at most every few hours."""
        with self.lock:
            entity = self.config["climate_entity"]
            due = self.learned_for != entity or now - self.learned_at >= (RATE_REFRESH_SECONDS if self.learned else RATE_RETRY_SECONDS)
        if not entity or not due:
            return
        try:
            rows = self.ha.history(entity, now - RATE_HISTORY_DAYS * 86400, now)
            points = []
            for row in rows:
                try:
                    a = row.get("attributes", {})
                    points.append({"t": parse_time(row["last_changed"]), "current": number_or_none(a.get("current_temperature")),
                                   "target": number_or_none(a.get("temperature")), "heating": a.get("hvac_action") == "heating"})
                except (KeyError, ValueError):
                    continue
            learned = learn_rates(sorted(points, key=lambda p: p["t"]))
        except HaError as exc:
            LOG.warning("Historiken för inlärda takter kunde inte läsas: %s", exc)
            learned = None
        with self.lock:
            if learned or self.learned_for != entity:
                self.learned = learned
            self.learned_at, self.learned_for = now, entity

    def load_settings(self):
        try:
            return validate(json.loads(self.settings_path.read_text()))
        except FileNotFoundError:
            return validate({})
        except (OSError, ValueError, PlanError) as exc:
            LOG.error("Ogiltiga sparade inställningar, använder standard: %s", exc)
            return validate({})

    def save_settings(self, raw):
        with self.lock:
            was_enabled = self.config["enabled"]
            config = validate({**self.config, **raw})
            self.write_settings(config)
            old_climate = self.config["climate_entity"]
            self.config = config
            self.committed = {}
            self.prices_at = 0
            if was_enabled and old_climate and (not config["enabled"] or old_climate != config["climate_entity"]):
                self.hand_back(old_climate, "Styrningen avstängd i inställningarna")
            elif config["enabled"] and not was_enabled:
                self.events.add("info", "Styrningen påslagen")
            self.last_write = 0
        return config

    def zone(self):
        if self.tz is None:
            self.tz = ZoneInfo(self.ha.time_zone())
        return self.tz

    def load_prices(self, now):
        if self.prices and now - self.prices_at < PRICE_CACHE_SECONDS:
            return self.prices
        try:
            self.prices, self.price_source = load_prices(self.ha, self.config["price_entity"], now, self.zone())
        except PriceError as exc:
            raise HaError(str(exc)) from exc
        self.prices_at = now
        return self.prices

    def follows(self, caps, climate, temperature):
        """True when the thermostat already runs at temperature (and in heat mode when it has one)."""
        if caps["heat"] and climate.get("state") != "heat":
            return False
        target = number_or_none(climate.get("attributes", {}).get("temperature"))
        if target is None:
            return False
        return abs(target - temperature) < 0.01 or self.equivalent.get(temperature) == target

    def fallback(self, caps):
        return "eget program" if caps["auto"] else f"fast {self.config['fallback_temp']:.1f} °C".replace(".", ",")

    def handed_back(self, caps, climate):
        if caps["auto"]:
            return climate.get("state") == "auto"
        return self.follows(caps, climate, setpoint(caps, self.config["fallback_temp"]))

    def hand_back(self, entity_id, reason):
        """Give the thermostat back: its own program (auto) when it has one, otherwise a fixed temperature."""
        try:
            climate = self.ha.state(entity_id)
            caps = thermostat_caps(climate.get("attributes", {}))
            if caps["auto"]:
                self.ha.service("climate", "set_hvac_mode", {"entity_id": entity_id, "hvac_mode": "auto"})
                self.events.add("info", f"{reason}: termostaten tillbaka på sitt eget program")
            else:
                value = setpoint(caps, self.config["fallback_temp"])
                data = {"entity_id": entity_id, "temperature": value}
                if caps["heat"]:
                    data["hvac_mode"] = "heat"
                self.ha.service("climate", "set_temperature", data)
                self.events.add("info", f"{reason}: termostaten saknar eget program, ställd på {degrees(value)} °C")
        except HaError as exc:
            self.events.add("error", f"{reason}: kunde inte lämna tillbaka termostaten ({exc})")

    def write_settings(self, config):
        tmp = self.settings_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(config, ensure_ascii=False, indent=2))
        tmp.replace(self.settings_path)

    def presence(self, config, now):
        """Track since when nobody is home. Off, unreadable or unknown presence counts as home."""
        entity = config["presence_entity"]
        home = None
        if entity:
            try:
                home = presence_home(entity, self.ha.state(entity).get("state"))
            except HaError as exc:
                LOG.warning("Närvarogivaren kunde inte läsas: %s", exc)
        if home is False:
            if self.away_since is None:
                self.away_since = now
                self.events.add("info", "Ingen hemma enligt närvarogivaren")
        else:
            if self.away_since is not None and home:
                self.events.add("info", "Någon är hemma igen – vanlig plan")
            self.away_since = None
        return {"entity": entity or None, "home": home, "away_since": self.away_since} if entity else None

    def end_vacation_if_over(self, now):
        away = vacation_bounds(self.config, self.zone())
        if away and now >= away[1]:
            self.config = {**self.config, "vacation": {**self.config["vacation"], "enabled": False}}
            self.write_settings(self.config)
            self.events.add("info", "Semestern är slut – vanliga komforttider gäller igen")

    def tick(self, now=None):
        now = time.time() if now is None else now
        self.refresh_rates(now)
        with self.lock:
            try:
                self.end_vacation_if_over(now)
            except HaError as exc:
                self.set_status({"error": f"Tidszonen kunde inte läsas: {exc}", "message": "Väntar"})
                return
            config = dict(self.config)
            if not config["climate_entity"] or not config["price_entity"]:
                self.set_status({"message": "Välj termostat och elprisgivare under Avancerat i inställningarna",
                                 "enabled": config["enabled"], "now": now, "version": VERSION, "setup": True})
                return
            try:
                climate = self.ha.state(config["climate_entity"])
            except HaError as exc:
                self.set_status({"error": f"Termostaten kunde inte läsas: {exc}", "message": "Väntar",
                                 "enabled": config["enabled"], "now": now, "version": VERSION})
                return
            attrs = climate.get("attributes", {})
            caps = thermostat_caps(attrs)
            current = number_or_none(attrs.get("current_temperature"))
            # A thermostat that stores a nearby value instead of the requested one (other step size).
            if self.last_request is not None:
                stored = number_or_none(attrs.get("temperature"))
                requested, self.last_request = self.last_request, None
                if stored is not None and stored != requested and abs(stored - requested) <= 1:
                    self.equivalent[requested] = stored
            available = climate.get("state") not in ("unavailable", "unknown")
            status = {"thermostat": {"mode": climate.get("state"), "target": number_or_none(attrs.get("temperature")),
                                     "current": current, "action": attrs.get("hvac_action"), "available": available,
                                     "fallback": self.fallback(caps), "step": caps["step"], "heat_mode": caps["heat"]},
                      "enabled": config["enabled"], "now": now, "version": VERSION}
            try:
                prices = self.load_prices(now)
            except HaError as exc:
                prices = []
                status["price_error"] = str(exc)
            # Drop committed preheats that have reached their comfort start.
            self.committed = {a: s for a, s in self.committed.items() if a > now}
            if not prices:
                status.update(message=f"Inga elpriser – termostaten kör {self.fallback(caps)}", plan=None)
                self.plan = None
                if config["enabled"] and available and not self.handed_back(caps, climate) and now - self.last_write >= MIN_WRITE_SECONDS:
                    self.last_write = now
                    self.hand_back(config["climate_entity"], "Elpriser saknas")
                self.set_status(status)
                return
            status["learned_rates"] = self.learned and {**self.learned, "in_use": bool(config["learn_rates"])}
            status["presence"] = self.presence(config, now)
            plan = build_plan(effective(config, self.learned), prices, now, self.zone(), current, committed_preheat=self.committed,
                              away_since=self.away_since)
            for item in plan["preheats"]:
                if item["start"] <= now < item["comfort_start"]:
                    self.committed[item["comfort_start"]] = item["start"]
            self.plan = plan
            row = plan["slots"][0]
            target = setpoint(caps, row["target"])
            upcoming = next_change(plan)
            status["price_source"] = PRICE_LABELS.get(self.price_source, self.price_source)
            status.update(target=row["target"], reason=row["reason"], price=row["price"], expensive=row["expensive"],
                          next_change=upcoming and {"t": upcoming["t"], "target": upcoming["target"], "reason": upcoming["reason"]})
            status["conflict"] = self.conflict(config)
            away = vacation_bounds(config, self.zone())
            status["vacation"] = {**config["vacation"], "active": bool(away and away[0] <= now < away[1])}
            if not config["enabled"]:
                status["message"] = "Visar bara plan – styrningen är avstängd"
            elif not available:
                status["message"] = "Termostaten är inte tillgänglig"
            elif self.follows(caps, climate, target):
                self.unconfirmed, self.stuck_reported = None, False
                status["message"] = "Termostaten följer planen"
            else:
                attempts = self.unconfirmed[1] if self.unconfirmed and self.unconfirmed[0] == target else 0
                stuck = attempts >= UNCONFIRMED_LIMIT
                if stuck and attempts == UNCONFIRMED_LIMIT and not self.stuck_reported:
                    self.stuck_reported = True
                    self.events.add("error", f"Termostaten tar inte emot {degrees(target)} °C – försöker igen var 30:e minut")
                if stuck:
                    status["error"] = (f"Termostaten har inte tagit emot {degrees(target)} °C efter {attempts} försök. "
                                       "Kontrollera termostaten och dess integration.")
                if now - self.last_write < (UNCONFIRMED_RETRY_SECONDS if stuck else MIN_WRITE_SECONDS):
                    status["message"] = "Väntar innan nästa försök" if stuck else "Väntar några minuter innan nästa ändring"
                else:
                    self.write_target(config, caps, target, row["reason"], attempts, status, now)
            self.set_status(status)

    def write_target(self, config, caps, target, reason, attempts, status, now):
        self.last_write = now
        try:
            data = {"entity_id": config["climate_entity"], "temperature": target}
            if caps["heat"]:
                data["hvac_mode"] = "heat"
            self.ha.service("climate", "set_temperature", data)
            self.last_request = target
            self.unconfirmed = (target, attempts + 1)
            if attempts == 0:
                self.stuck_reported = False
                self.events.add("change", f"{degrees(target)} °C – {reason}")
            status["message"] = f"Ställde termostaten på {degrees(target)} °C"
        except HaError as exc:
            self.events.add("error", f"Kunde inte ställa termostaten: {exc}")
            status["message"] = "Kunde inte ställa termostaten"
            status["error"] = str(exc)

    def set_status(self, status):
        self.status = status
        self.publish(status)

    def conflict(self, config):
        entity = config["conflict_automation"]
        if not entity:
            return None
        try:
            state = self.ha.state(entity)
        except HaError:
            return None
        if state.get("state") == "on":
            return {"entity_id": entity, "name": state.get("attributes", {}).get("friendly_name", entity)}
        return None

    def disable_conflict(self):
        entity = self.config["conflict_automation"]
        if entity:
            self.ha.service("automation", "turn_off", {"entity_id": entity})
            self.events.add("info", f"Stängde av {entity}")

    def publish(self, status):
        """Expose the plan as Home Assistant sensors (best effort)."""
        if os.environ.get("NO_PUBLISH") == "1":
            return
        upcoming = status.get("next_change") or {}
        sensors = {
            "sensor.golvvarme_comfort_mal": (status.get("target"), {
                "unit_of_measurement": "°C", "device_class": "temperature", "friendly_name": "Golvvärme Comfort mål",
                "reason": status.get("reason"), "next_target": upcoming.get("target"),
                "next_change": upcoming.get("t") and datetime.fromtimestamp(upcoming["t"], self.zone()).isoformat()}),
            "sensor.golvvarme_comfort_status": (status.get("message"), {
                "friendly_name": "Golvvärme Comfort status", "enabled": status.get("enabled"), "icon": "mdi:heating-coil"}),
        }
        for entity_id, (state, attributes) in sensors.items():
            try:
                self.ha.request(f"/states/{entity_id}", {"state": "unknown" if state is None else state,
                                                         "attributes": attributes})
            except HaError:
                pass

    def snapshot(self):
        with self.lock:
            return {**self.status, "plan": self.plan and {"slots": self.plan["slots"], "windows": self.plan["windows"],
                                                          "preheats": self.plan["preheats"]}}

    def history(self):
        """Past 24 h of thermostat readings and targets, and today's heating summary, for the chart."""
        hours = 24
        now = time.time()
        with self.lock:
            config = dict(self.config)
            cached_at, cached = self.summary_cache
            if cached and now - cached_at < 300:
                return cached
        start = now - hours * 3600
        rows = self.ha.history(config["climate_entity"], start, now)
        points = []
        for row in rows:
            try:
                a = row.get("attributes", {})
                points.append({"t": parse_time(row["last_changed"]), "current": number_or_none(a.get("current_temperature")),
                               "target": number_or_none(a.get("temperature")), "mode": row.get("state"),
                               "heating": a.get("hvac_action") == "heating"})
            except (KeyError, ValueError):
                continue
        local = datetime.fromtimestamp(now, self.zone())
        midnight = local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        try:
            prices = self.load_prices(now)
        except HaError:
            prices = []
        result = {"points": points,
                  "today": heating_summary(rows, prices, config["power_kw"], midnight, now) if midnight >= start else None,
                  "last24": heating_summary(rows, prices, config["power_kw"], start, now),
                  "prices": [p for p in prices if p[0] >= start - 3600]}
        with self.lock:
            self.summary_cache = (now, result)
        return result

    def entities(self):
        states = self.ha.states()
        price_ids = {s["entity_id"] for s in states if "/kwh" in str(s["attributes"].get("unit_of_measurement", "")).lower()
                     or "/mwh" in str(s["attributes"].get("unit_of_measurement", "")).lower()}
        classes = {s["entity_id"]: s["attributes"].get("device_class") for s in states}
        pick = lambda prefix: sorted(({"id": s["entity_id"], "name": s["attributes"].get("friendly_name", s["entity_id"])}
                                      for s in states if s["entity_id"].startswith(prefix)), key=lambda e: e["name"].lower())
        return {"climate": pick("climate."),
                "price": [e for e in pick("sensor.") if e["id"] in price_ids],
                "automation": pick("automation."),
                # Binary sensors only when they describe occupancy; motion sensors flicker too much.
                "presence": [e for prefix in PRESENCE_DOMAINS for e in pick(prefix)
                             if prefix != "binary_sensor." or classes.get(e["id"]) in ("occupancy", "presence")]}


def handler_for(controller, open_access=False):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            LOG.debug("%s %s", self.address_string(), fmt % args)

        def respond(self, code, value, content_type="application/json; charset=utf-8"):
            body = json.dumps(value, ensure_ascii=False).encode() if isinstance(value, (dict, list)) else value
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; frame-ancestors 'self'")
            self.end_headers()
            self.wfile.write(body)

        def allowed(self):
            if open_access or self.client_address[0] == "172.30.32.2":
                if self.command == "POST" and self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                    self.respond(415, {"error": "Content-Type application/json krävs."})
                    return False
                return True
            self.respond(403, {"error": "Använd Home Assistants ingress."})
            return False

        def do_GET(self):
            if not self.allowed():
                return
            path = urlsplit(self.path).path
            try:
                if path == "/api/status":
                    self.respond(200, controller.snapshot())
                elif path == "/api/settings":
                    self.respond(200, controller.config)
                elif path == "/api/community":
                    self.respond(200, {"community": COMMUNITY.get()})
                elif path == "/api/defaults":
                    self.respond(200, DEFAULTS)
                elif path == "/api/history":
                    self.respond(200, controller.history())
                elif path == "/api/events":
                    self.respond(200, controller.events.recent())
                elif path == "/api/entities":
                    self.respond(200, controller.entities())
                else:
                    name = "index.html" if path in ("/", "") else path.lstrip("/")
                    file = (STATIC / name).resolve()
                    if file.parent != STATIC.resolve() or not file.is_file():
                        self.respond(404, {"error": "Finns inte"})
                        return
                    types = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                             ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml"}
                    self.respond(200, file.read_bytes(), types.get(file.suffix, "application/octet-stream"))
            except HaError as exc:
                self.respond(502, {"error": str(exc)})

        def do_POST(self):
            if not self.allowed():
                return
            path = urlsplit(self.path).path
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length > MAX_BODY:
                    self.respond(413, {"error": "För stor begäran."})
                    return
                body = json.loads(self.rfile.read(length)) if length > 0 else {}
                if path == "/api/settings":
                    if not isinstance(body, dict):
                        raise PlanError("Inställningarna måste vara ett objekt.")
                    config = controller.save_settings(body)
                    threading.Thread(target=controller.tick, daemon=True).start()
                    self.respond(200, config)
                elif path == "/api/disable-conflict":
                    controller.disable_conflict()
                    self.respond(200, {"ok": True})
                else:
                    self.respond(404, {"error": "Finns inte"})
            except PlanError as exc:
                self.respond(400, {"error": str(exc)})
            except HaError as exc:
                self.respond(502, {"error": str(exc)})
            except ValueError:
                self.respond(400, {"error": "Ogiltig JSON."})

    return Handler


def run_loop(controller, stop, interval=60):
    while not stop.is_set():
        try:
            controller.tick()
        except Exception:
            LOG.exception("Oväntat fel i styrloopen")
            with controller.lock:
                controller.set_status({"error": "Internt fel i styrloopen, försöker igen om en minut.", "message": "Pausad"})
        stop.wait(interval)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    token = os.environ.get("HA_TOKEN") or os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        raise SystemExit("SUPERVISOR_TOKEN saknas")
    ha = HomeAssistant(token, os.environ.get("HA_URL", "http://supervisor/core/api"))
    data_dir = Path(os.environ.get("DATA_DIR", "/data"))
    data_dir.mkdir(parents=True, exist_ok=True)
    controller = Controller(ha, data_dir)
    stop = threading.Event()
    port = int(os.environ.get("PORT", "8097"))
    server = ThreadingHTTPServer(("0.0.0.0", port), handler_for(controller, open_access=os.environ.get("OPEN_ACCESS") == "1"))

    def shutdown(*_):
        stop.set()
        with controller.lock:
            if controller.config["enabled"] and controller.config["climate_entity"]:
                controller.hand_back(controller.config["climate_entity"], "Tillägget stoppas")
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    threading.Thread(target=run_loop, args=(controller, stop), daemon=True).start()
    LOG.info("Golvvärme Comfort %s lyssnar på %s", VERSION, port)
    server.serve_forever()


if __name__ == "__main__":
    main()
