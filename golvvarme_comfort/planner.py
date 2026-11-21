"""Price-aware plan for an electric floor heating thermostat. Pure functions, no I/O."""

import math
from datetime import datetime, timedelta

SLOT = 15 * 60
# Expensive or cheap runs shorter than this are ignored: a floor holds its heat for hours, so a
# 15-minute dip saves nothing, while each change costs two writes to a cloud thermostat.
MIN_RUN_SLOTS = 4
DAYS = ["mån", "tis", "ons", "tor", "fre", "lör", "sön"]
PRESENCE_DOMAINS = ("person.", "zone.", "group.", "binary_sensor.", "input_boolean.")


def presence_home(entity_id, state):
    """True home, False away, None unknown for a presence entity's state.

    person/group: home vs anything else (not_home or another zone); zone: number of people in it;
    binary_sensor/input_boolean: on vs off. Unknown or unavailable never counts as away."""
    if state in (None, "", "unknown", "unavailable"):
        return None
    if entity_id.startswith("zone."):
        try:
            return float(state) > 0
        except ValueError:
            return None
    if entity_id.startswith(("binary_sensor.", "input_boolean.")):
        return {"on": True, "off": False}.get(state)
    return state == "home"


def nearest_person(home, states):
    """(km, name) of the person entity closest to home by its GPS position, or None.

    home: (latitude, longitude). Persons without a position (no GPS tracker) are skipped."""
    best = None
    for s in states:
        if not s.get("entity_id", "").startswith("person."):
            continue
        attrs = s.get("attributes") or {}
        try:
            lat, lon = float(attrs["latitude"]), float(attrs["longitude"])
        except (KeyError, TypeError, ValueError):
            continue
        km = distance_km(home, (lat, lon))
        if best is None or km < best[0]:
            best = (km, attrs.get("friendly_name") or s["entity_id"])
    return best


def distance_km(a, b):
    """Great-circle distance between two (latitude, longitude) points."""
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(h))

DEFAULTS = {
    "enabled": False,
    "climate_entity": "",
    "price_entity": "",
    "conflict_automation": "",
    "comfort_temp": 21.0,
    "economy_temp": 17.0,
    "setback_temp": 16.0,
    "comfort_periods": [
        {"start": "05:00", "end": "06:00", "days": [0, 1, 2, 3, 4, 5, 6]},
        {"start": "16:30", "end": "21:00", "days": [0, 1, 2, 3, 4, 5, 6]},
    ],
    "heatup_rate": 1.0,
    "cooldown_rate": 0.45,
    "learn_rates": False,
    "max_preheat_hours": 3.0,
    "holding_share": 0.3,
    "expensive_mode": "relative",
    "expensive_percent": 75,
    "expensive_price": 2.8,
    "expensive_min_price": 1.0,
    "comfort_reduction": 1.0,
    # Also lower a comfort period by comfort_reduction when its preheating has to run in expensive hours.
    "price_aware_preheat": True,
    "cheap_boost_below": 0.0,
    # Presence only ever lowers: nobody home -> no preheating and economy instead of comfort.
    "presence_entity": "",
    "presence_grace_minutes": 15.0,
    "presence_long_away_hours": 4.0,
    # Away but someone within this many km of home (by phone GPS) counts as home; 0 = off.
    "presence_near_km": 0.0,
    "power_kw": 1.0,
    "fallback_temp": 20.0,
    # Anonymous ping {app, random id, version} every 6 h for the installation count; see community.py.
    "share_usage": True,
    "vacation": {"enabled": False, "start": "", "end": "", "temp": 15.0},
}


class PlanError(Exception):
    pass


def _number(value, label, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PlanError(f"{label} måste vara ett tal.")
    if not low <= value <= high:
        raise PlanError(f"{label} måste vara mellan {low} och {high}.")
    return float(value)


def minutes(text, label):
    try:
        hour, minute = (int(part) for part in str(text).split(":"))
    except ValueError as exc:
        raise PlanError(f"{label}: ange tid som TT:MM.") from exc
    if not (0 <= hour <= 24 and 0 <= minute < 60) or hour * 60 + minute > 1440:
        raise PlanError(f"{label}: ogiltig tid {text}.")
    return hour * 60 + minute


def validate(raw):
    """A complete, checked settings dict from defaults plus the given values."""
    unknown = set(raw) - set(DEFAULTS)
    if unknown:
        raise PlanError(f"Okända inställningar: {', '.join(sorted(unknown))}.")
    config = {**DEFAULTS, **raw}
    if not isinstance(config["enabled"], bool):
        raise PlanError("Styr termostaten måste vara på eller av.")
    if not isinstance(config["learn_rates"], bool):
        raise PlanError("Inlärda takter måste vara på eller av.")
    if not isinstance(config["price_aware_preheat"], bool):
        raise PlanError("Sänkning vid dyr förvärmning måste vara på eller av.")
    if not isinstance(config["share_usage"], bool):
        raise PlanError("Anonym användningsstatistik måste vara på eller av.")
    for key, prefix in (("climate_entity", "climate."), ("price_entity", "sensor."), ("conflict_automation", "automation.")):
        value = config[key]
        if not isinstance(value, str) or (value and not value.startswith(prefix)):
            raise PlanError(f"{key} måste vara en {prefix}-entitet.")
    presence = config["presence_entity"]
    if not isinstance(presence, str) or (presence and not presence.startswith(PRESENCE_DOMAINS)):
        raise PlanError("Närvarogivaren måste vara en person, zon, grupp, binary_sensor eller input_boolean.")
    for key, label, low, high in (
        ("comfort_temp", "Komforttemperatur", 5, 35), ("economy_temp", "Ekonomitemperatur", 5, 35),
        ("setback_temp", "Sänkt temperatur vid dyr el", 5, 35), ("heatup_rate", "Uppvärmningstakt", 0.1, 10), ("cooldown_rate", "Avsvalning", 0, 5),
        ("max_preheat_hours", "Längsta förvärmning", 0, 12), ("holding_share", "Hålleffekt", 0, 1),
        ("expensive_percent", "Dyr-gräns (procent)", 50, 100), ("expensive_price", "Dyr-gräns (kr)", 0, 100),
        ("expensive_min_price", "Lägsta dyra pris", 0, 100), ("comfort_reduction", "Sänkning under komforttid", 0, 10),
        ("cheap_boost_below", "Extra värme under", 0, 100), ("power_kw", "Effekt", 0.05, 20),
        ("fallback_temp", "Reservtemperatur", 5, 35),
        ("presence_grace_minutes", "Borta minst (min)", 0, 240), ("presence_long_away_hours", "Länge borta (h)", 0.5, 72),
        ("presence_near_km", "Nära hemmet (km)", 0, 500),
    ):
        config[key] = _number(config[key], label, low, high)
    if not config["setback_temp"] <= config["economy_temp"] <= config["comfort_temp"]:
        raise PlanError("Temperaturerna måste vara sänkt ≤ ekonomi ≤ komfort.")
    if config["expensive_mode"] not in ("relative", "absolute"):
        raise PlanError("Dyr el-läge måste vara relative eller absolute.")
    periods = config["comfort_periods"]
    if not isinstance(periods, list) or len(periods) > 12:
        raise PlanError("Komforttider måste vara en lista med högst 12 perioder.")
    clean = []
    for index, period in enumerate(periods, 1):
        if not isinstance(period, dict):
            raise PlanError(f"Komfortperiod {index} är ogiltig.")
        start, end = minutes(period.get("start"), f"Period {index} start"), minutes(period.get("end"), f"Period {index} slut")
        if start == end:
            raise PlanError(f"Period {index}: start och slut kan inte vara samma tid.")
        days = period.get("days", list(range(7)))
        if not isinstance(days, list) or not days or any(d not in range(7) or isinstance(d, bool) for d in days):
            raise PlanError(f"Period {index}: välj minst en veckodag.")
        item = {"start": period["start"], "end": period["end"], "days": sorted(set(days))}
        if period.get("temp") is not None:
            item["temp"] = _number(period["temp"], f"Period {index} temperatur", 5, 35)
            if item["temp"] < config["economy_temp"]:
                raise PlanError(f"Period {index}: temperaturen kan inte vara lägre än ekonomitemperaturen.")
        clean.append(item)
    config["comfort_periods"] = clean
    config["vacation"] = validate_vacation(config["vacation"])
    return config


def local_stamp(text, label):
    try:
        return datetime.fromisoformat(str(text)).replace(second=0, microsecond=0, tzinfo=None)
    except ValueError as exc:
        raise PlanError(f"{label}: ange datum och tid som ÅÅÅÅ-MM-DDTTT:MM.") from exc


def validate_vacation(raw):
    """Vacation: a local start and end (ISO without time zone) and the temperature to keep meanwhile."""
    if not isinstance(raw, dict) or set(raw) - {"enabled", "start", "end", "temp"}:
        raise PlanError("Semesterinställningen är ogiltig.")
    vacation = {**DEFAULTS["vacation"], **raw}
    if not isinstance(vacation["enabled"], bool):
        raise PlanError("Semester måste vara på eller av.")
    vacation["temp"] = _number(vacation["temp"], "Semestertemperatur", 5, 35)
    if vacation["enabled"]:
        start, end = local_stamp(vacation["start"], "Semester från"), local_stamp(vacation["end"], "Semester till")
        if end <= start:
            raise PlanError("Semestern måste sluta efter att den börjar.")
        vacation["start"], vacation["end"] = start.isoformat(timespec="minutes"), end.isoformat(timespec="minutes")
    elif not all(isinstance(vacation[k], str) for k in ("start", "end")):
        raise PlanError("Semesterns datum är ogiltiga.")
    return vacation


def vacation_bounds(config, tz):
    """(start, end) epoch of an enabled vacation, else None."""
    vacation = config.get("vacation") or {}
    if not vacation.get("enabled"):
        return None
    to_epoch = lambda text: local_stamp(text, "Semester").replace(tzinfo=tz).timestamp()
    return to_epoch(vacation["start"]), to_epoch(vacation["end"])


def comfort_spans(config, start, end, tz):
    """(start, end, temperature) of each comfort period that overlaps [start, end), unmerged. Periods may pass midnight."""
    spans = []
    day = datetime.fromtimestamp(start, tz).date() - timedelta(days=1)
    last = datetime.fromtimestamp(end, tz).date()
    while day <= last:
        for period in config["comfort_periods"]:
            if day.weekday() not in period["days"]:
                continue
            begin, finish = minutes(period["start"], ""), minutes(period["end"], "")
            midnight = datetime(day.year, day.month, day.day, tzinfo=tz)
            a = (midnight + timedelta(minutes=begin)).timestamp()
            b = (midnight + timedelta(minutes=finish + (1440 if finish < begin else 0))).timestamp()
            if b > start and a < end:
                spans.append((a, b, period.get("temp", config["comfort_temp"])))
        day += timedelta(days=1)
    return spans


def comfort_windows(config, start, end, tz, spans=None):
    """(start, end) epoch pairs of comfort periods that overlap [start, end), overlapping periods merged."""
    if spans is None:
        spans = comfort_spans(config, start, end, tz)
    merged = []
    for a, b, _temp in sorted(spans):
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    return merged


def price_at(prices, stamp):
    """Price of the interval containing stamp; prices are sorted (start, value) with 15- or 60-min steps."""
    found = None
    for index, (start, value) in enumerate(prices):
        if start > stamp:
            break
        found = index
    if found is None:
        return None
    start, value = prices[found]
    if found + 1 < len(prices):
        length = prices[found + 1][0] - start
    else:
        length = start - prices[found - 1][0] if found > 0 else 3600
    return value if stamp - start < min(length, 3600) else None


def expensive_flags(config, slots, prices, tz):
    """Per slot: True when the price counts as expensive."""
    flags = []
    by_day = {}
    if config["expensive_mode"] == "relative":
        for start, value in prices:
            by_day.setdefault(datetime.fromtimestamp(start, tz).date(), []).append(value)
    for stamp, price in slots:
        if price is None:
            flags.append(False)
        elif config["expensive_mode"] == "absolute":
            flags.append(price >= config["expensive_price"])
        else:
            values = by_day.get(datetime.fromtimestamp(stamp, tz).date(), [])
            rank = sum(v < price for v in values) / (len(values) - 1) if len(values) > 1 else 0
            flags.append(rank * 100 >= config["expensive_percent"] and price >= config["expensive_min_price"])
    return flags


def price_is_expensive(config, price, stamp, prices, tz):
    """The expensive rule of expensive_flags for one price on the day of stamp (e.g. a preheat average)."""
    if price is None:
        return False
    if config["expensive_mode"] == "absolute":
        return price >= config["expensive_price"]
    day = datetime.fromtimestamp(stamp, tz).date()
    values = [v for start, v in prices if datetime.fromtimestamp(start, tz).date() == day]
    rank = sum(v < price for v in values) / (len(values) - 1) if len(values) > 1 else 0
    return rank * 100 >= config["expensive_percent"] and price >= config["expensive_min_price"]


def drop_short_runs(flags, min_slots, open_ends):
    """flags with True runs shorter than min_slots set to False.

    open_ends[i] is True when slot i lies outside the known prices (or before the plan); a run that
    touches such a slot may continue there, so it is kept."""
    result = list(flags)
    i = 0
    while i < len(result):
        if not result[i]:
            i += 1
            continue
        j = i
        while j < len(result) and result[j]:
            j += 1
        touches_open = (i == 0 or open_ends[i - 1]) or (j == len(result) or open_ends[j])
        if j - i < min_slots and not touches_open:
            result[i:j] = [False] * (j - i)
        i = j
    return result


def best_preheat_start(config, slots, index_of, comfort_start, hours_needed, now):
    """Start time of preheating before comfort_start that gives the lowest estimated cost.

    Heating at full power for hours_needed, then holding (holding_share of full power) until comfort_start.
    Ties and missing prices fall back to the latest start, which is what a plain thermostat does."""
    latest = comfort_start - round(hours_needed * 3600 / SLOT) * SLOT
    earliest = comfort_start - round(config["max_preheat_hours"] * 3600 / SLOT) * SLOT
    earliest = max(earliest, now - (now % SLOT))
    if latest <= earliest:
        return max(earliest, latest) if latest < comfort_start else comfort_start
    best, best_cost = latest, None
    candidate = latest
    while candidate >= earliest:
        cost, missing = 0.0, False
        t = candidate
        while t < comfort_start:
            i = index_of.get(t)
            price = slots[i][1] if i is not None else None
            if price is None:
                missing = True
                break
            share = 1.0 if t < candidate + hours_needed * 3600 else config["holding_share"]
            cost += price * share
            t += SLOT
        if missing:
            break
        if best_cost is None or cost < best_cost - 1e-9:
            best, best_cost = candidate, cost
        candidate -= SLOT
    return best


def predicted_temperature(config, plan, windows, position, current_temp, first, comfort_at=None):
    """Expected floor temperature at the start of windows[position] without preheating.

    The floor cools by cooldown_rate from the current reading (or from comfort at the end of an
    earlier window) but not below the lowest target of the gap before the window."""
    a = windows[position][0]
    previous = [w for w in windows[:position] if w[1] > first]
    if previous:
        end_temp = comfort_at(previous[-1][1] - SLOT) if comfort_at else None
        base_temp, base_time = end_temp or config["comfort_temp"], previous[-1][1]
    elif current_temp is not None:
        base_temp, base_time = current_temp, first
    else:
        base_temp, base_time = config["economy_temp"], first
    gap = [row["target"] for row in plan if base_time <= row["t"] < a]
    floor = min(gap) if gap else config["economy_temp"]
    return max(floor, base_temp - config["cooldown_rate"] * max(0.0, a - base_time) / 3600)


def build_plan(config, prices, now, tz, current_temp=None, horizon_hours=36, committed_preheat=None, away_since=None):
    """Slots from now: {t, price, target, reason, expensive, comfort}.

    committed_preheat: {comfort_start: preheat_start} already begun; kept so a changing room
    temperature cannot move a running preheat back and forth.
    away_since: epoch since nobody is home (None: home or unknown). After presence_grace_minutes the
    plan keeps economy instead of comfort and skips preheating, and after presence_long_away_hours
    it keeps the setback temperature. The plan is rebuilt every minute, so coming home restores it."""
    away_for = None if away_since is None else now - away_since
    nobody_home = away_for is not None and away_for >= config["presence_grace_minutes"] * 60
    long_away = nobody_home and away_for >= config["presence_long_away_hours"] * 3600
    first = now - (now % SLOT)
    if prices:
        step = prices[-1][0] - prices[-2][0] if len(prices) > 1 else 3600
        prices_end = prices[-1][0] + step
    else:
        prices_end = first + SLOT
    end = min(first + horizon_hours * 3600, max(prices_end, first + 3600 * 6))
    stamps = list(range(int(first), int(end), SLOT))
    slots = [(t, price_at(prices, t)) for t in stamps]
    index_of = {t: i for i, t in enumerate(stamps)}
    # Look back a little so a run that began before now keeps its true length.
    back = [(t, price_at(prices, t)) for t in range(int(first) - (MIN_RUN_SLOTS - 1) * SLOT, int(first), SLOT)]
    extended = back + slots
    open_ends = [price is None for _t, price in extended]
    expensive_all = drop_short_runs(expensive_flags(config, extended, prices, tz), MIN_RUN_SLOTS, open_ends)
    cheap_all = drop_short_runs(
        [price is not None and config["cheap_boost_below"] > 0 and price <= config["cheap_boost_below"] and not expensive
         for (_t, price), expensive in zip(extended, expensive_all)], MIN_RUN_SLOTS, open_ends)
    flags, cheap = expensive_all[len(back):], cheap_all[len(back):]
    spans = comfort_spans(config, first, end + config["max_preheat_hours"] * 3600, tz)
    windows = comfort_windows(config, first, end, tz, spans)

    def comfort_at(t):
        """Comfort temperature at t: the warmest period covering it (None outside comfort)."""
        temps = [temp for a, b, temp in spans if a <= t < b]
        return max(temps) if temps else None
    away = vacation_bounds(config, tz)
    if away:
        clipped = []
        for a, b in windows:
            if b <= away[0] or a >= away[1]:
                clipped.append((a, b))
                continue
            if a < away[0]:
                clipped.append((a, away[0]))
            if b > away[1]:
                clipped.append((away[1], b))
        windows = clipped

    plan = []
    for (t, price), expensive, boost in zip(slots, flags, cheap):
        in_comfort = any(a <= t < b for a, b in windows)
        if away and away[0] <= t < away[1]:
            target, reason = config["vacation"]["temp"], "Semester"
        elif long_away:
            target, reason = config["setback_temp"], "Ingen hemma sedan länge – sänkt"
        elif nobody_home:
            target, reason = config["economy_temp"], "Ingen hemma – ekonomi"
            if expensive:
                target, reason = config["setback_temp"], "Ingen hemma, dyr el – sänkt"
        elif in_comfort:
            full = comfort_at(t) or config["comfort_temp"]
            target, reason = full, "Komforttid"
            if expensive and config["comfort_reduction"] > 0:
                target, reason = max(config["economy_temp"], full - config["comfort_reduction"]), "Komforttid, dyr el – något sänkt"
        else:
            target, reason = config["economy_temp"], "Ekonomi"
            if expensive:
                target, reason = config["setback_temp"], "Dyr el – sänkt"
            elif boost:
                target, reason = config["comfort_temp"], "Billig el – extra värme"
        plan.append({"t": t, "price": price, "target": round(target, 1), "reason": reason,
                     "expensive": expensive, "comfort": in_comfort})

    preheats = []
    committed_preheat = committed_preheat or {}
    for position, (a, b) in enumerate(windows):
        if a <= first or nobody_home:
            continue
        full = comfort_at(a) or config["comfort_temp"]
        goal = next((row["target"] for row in plan if row["t"] == a), full)
        if a in committed_preheat:
            start = committed_preheat[a]
        else:
            from_temp = predicted_temperature(config, plan, windows, position, current_temp, first, comfort_at)
            needed = min(max(0.0, goal - from_temp) / config["heatup_rate"], config["max_preheat_hours"])
            if needed <= 0:
                continue
            start = best_preheat_start(config, slots, index_of, a, needed, now)
            # Reaching full comfort would mean heating in expensive hours: aim a little lower instead.
            reduced = max(config["economy_temp"], goal - config["comfort_reduction"])
            if config["price_aware_preheat"] and start < a and reduced < goal and goal >= full - 1e-9:
                paid = [slots[index_of[t]][1] for t in range(int(start), int(a), SLOT) if t in index_of]
                paid = [p for p in paid if p is not None]
                if paid and price_is_expensive(config, sum(paid) / len(paid), a, prices, tz):
                    goal = reduced
                    for row in plan:
                        if a <= row["t"] < b and row["comfort"] and row["target"] > goal:
                            row["target"], row["reason"] = goal, "Komforttid – dyr förvärmning, något sänkt"
                    needed = min(max(0.0, goal - from_temp) / config["heatup_rate"], config["max_preheat_hours"])
                    if needed <= 0:
                        continue
                    start = best_preheat_start(config, slots, index_of, a, needed, now)
        if start >= a:
            continue
        preheats.append({"start": start, "comfort_start": a})
        label = datetime.fromtimestamp(a, tz).strftime("%H:%M")
        for row in plan:
            if start <= row["t"] < a and row["target"] < goal:
                row["target"], row["reason"] = goal, f"Förvärmer inför {label}"
    return {"slots": plan, "preheats": preheats, "windows": windows}


def learn_rates(points, min_minutes=45, min_runs=3):
    """Heat-up and cool-down rates (°C/h) measured from the thermostat's own history.

    points: [{t, current, target, heating}] in time order. A heating run gives a heat-up rate when the
    floor rose; an idle run with the target at least 0.5 °C below the floor gives a cool-down rate.
    Runs shorter than min_minutes or moving less than 0.5 °C are skipped (0.5 °C readings). The median of each needs min_runs runs."""
    runs, current = [], None
    for point in points:
        if point.get("current") is None:
            current = None
            continue
        if current is None or point["heating"] != current["heating"]:
            current = {"heating": point["heating"], "first": point, "last": point}
            runs.append(current)
        else:
            current["last"] = point
    heat, cool = [], []
    for run in runs:
        first, last = run["first"], run["last"]
        hours = (last["t"] - first["t"]) / 3600
        if hours * 60 < min_minutes:
            continue
        change = last["current"] - first["current"]
        rate = change / hours
        # Holding at the target cycles the heater with a flat floor; only real warm-ups and cool-downs count.
        if abs(change) < 0.5:
            continue
        if run["heating"] and rate > 0:
            heat.append(rate)
        elif not run["heating"] and rate < 0 and first.get("target") is not None and first["target"] <= first["current"] - 0.5:
            cool.append(-rate)
    median = lambda values: sorted(values)[len(values) // 2] if len(values) >= min_runs else None
    heatup, cooldown = median(heat), median(cool)
    return {"heatup_rate": None if heatup is None else round(min(10.0, max(0.1, heatup)), 2),
            "cooldown_rate": None if cooldown is None else round(min(5.0, cooldown), 2),
            "heat_runs": len(heat), "cool_runs": len(cool)}


def effective(config, learned):
    """Settings with learned rates in place of the typed ones when learning is on and a rate is known."""
    if not config.get("learn_rates") or not learned:
        return config
    return {**config, **{key: learned[key] for key in ("heatup_rate", "cooldown_rate") if learned.get(key) is not None}}


def next_change(plan):
    slots = plan["slots"]
    for row in slots[1:]:
        if row["target"] != slots[0]["target"]:
            return row
    return None

