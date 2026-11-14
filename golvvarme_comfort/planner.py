"""Price-aware plan for an electric floor heating thermostat. Pure functions, no I/O."""

from datetime import datetime, timedelta

SLOT = 15 * 60
# Expensive or cheap runs shorter than this are ignored: a floor holds its heat for hours, so a
# 15-minute dip saves nothing, while each change costs two writes to a cloud thermostat.
MIN_RUN_SLOTS = 4
DAYS = ["mån", "tis", "ons", "tor", "fre", "lör", "sön"]

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
    "max_preheat_hours": 3.0,
    "holding_share": 0.3,
    "expensive_mode": "relative",
    "expensive_percent": 75,
    "expensive_price": 2.8,
    "expensive_min_price": 1.0,
    "comfort_reduction": 1.0,
    "cheap_boost_below": 0.0,
    "power_kw": 1.0,
    "fallback_temp": 20.0,
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
    for key, prefix in (("climate_entity", "climate."), ("price_entity", "sensor."), ("conflict_automation", "automation.")):
        value = config[key]
        if not isinstance(value, str) or (value and not value.startswith(prefix)):
            raise PlanError(f"{key} måste vara en {prefix}-entitet.")
    for key, label, low, high in (
        ("comfort_temp", "Komforttemperatur", 5, 35), ("economy_temp", "Ekonomitemperatur", 5, 35),
        ("setback_temp", "Sänkt temperatur vid dyr el", 5, 35), ("heatup_rate", "Uppvärmningstakt", 0.1, 10), ("cooldown_rate", "Avsvalning", 0, 5),
        ("max_preheat_hours", "Längsta förvärmning", 0, 12), ("holding_share", "Hålleffekt", 0, 1),
        ("expensive_percent", "Dyr-gräns (procent)", 50, 100), ("expensive_price", "Dyr-gräns (kr)", 0, 100),
        ("expensive_min_price", "Lägsta dyra pris", 0, 100), ("comfort_reduction", "Sänkning under komforttid", 0, 10),
        ("cheap_boost_below", "Extra värme under", 0, 100), ("power_kw", "Effekt", 0.05, 20),
        ("fallback_temp", "Reservtemperatur", 5, 35),
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
        clean.append({"start": period["start"], "end": period["end"], "days": sorted(set(days))})
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


def comfort_windows(config, start, end, tz):
    """(start, end) epoch pairs of comfort periods that overlap [start, end). Periods may pass midnight."""
    windows = []
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
                windows.append((a, b))
        day += timedelta(days=1)
    merged = []
    for a, b in sorted(windows):
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


def predicted_temperature(config, plan, windows, position, current_temp, first):
    """Expected floor temperature at the start of windows[position] without preheating.

    The floor cools by cooldown_rate from the current reading (or from comfort at the end of an
    earlier window) but not below the lowest target of the gap before the window."""
    a = windows[position][0]
    previous = [w for w in windows[:position] if w[1] > first]
    if previous:
        base_temp, base_time = config["comfort_temp"], previous[-1][1]
    elif current_temp is not None:
        base_temp, base_time = current_temp, first
    else:
        base_temp, base_time = config["economy_temp"], first
    gap = [row["target"] for row in plan if base_time <= row["t"] < a]
    floor = min(gap) if gap else config["economy_temp"]
    return max(floor, base_temp - config["cooldown_rate"] * max(0.0, a - base_time) / 3600)


def build_plan(config, prices, now, tz, current_temp=None, horizon_hours=36, committed_preheat=None):
    """Slots from now: {t, price, target, reason, expensive, comfort}.

    committed_preheat: {comfort_start: preheat_start} already begun; kept so a changing room
    temperature cannot move a running preheat back and forth."""
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
    windows = comfort_windows(config, first, end + config["max_preheat_hours"] * 3600, tz)
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
        elif in_comfort:
            target, reason = config["comfort_temp"], "Komforttid"
            if expensive and config["comfort_reduction"] > 0:
                target, reason = config["comfort_temp"] - config["comfort_reduction"], "Komforttid, dyr el – något sänkt"
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
    for position, (a, _b) in enumerate(windows):
        if a <= first:
            continue
        goal = next((row["target"] for row in plan if row["t"] == a), config["comfort_temp"])
        if a in committed_preheat:
            start = committed_preheat[a]
        else:
            from_temp = predicted_temperature(config, plan, windows, position, current_temp, first)
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


def next_change(plan):
    slots = plan["slots"]
    for row in slots[1:]:
        if row["target"] != slots[0]["target"]:
            return row
    return None

