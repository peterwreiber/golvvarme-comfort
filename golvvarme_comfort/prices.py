"""Electricity prices from Tibber or Nord Pool (official integration or the HACS nordpool sensor)."""

import re
from datetime import datetime, timedelta

AREA = re.compile(r"(?:^|[_\s])(se[1-4]|no[1-5]|dk[12]|fi|ee|lv|lt|sys)(?:$|[_\s])", re.I)
SOURCE_TEMPLATE = (
    "{% set e = '__ENTITY__' %}"
    "{% set p = 'tibber' if e in integration_entities('tibber') else "
    "'nordpool' if e in integration_entities('nordpool') else '' %}"
    "{{ {'platform': p, 'entry': config_entry_id(e), "
    "'device': device_attr(device_id(e), 'name') if device_id(e) else none} | to_json }}"
)
LABELS = {"tibber": "Tibber", "nordpool": "Nord Pool (spotpris)", "nordpool_hacs": "Nord Pool (HACS)"}


class PriceError(Exception):
    pass


def parse_time(text, label="Pris"):
    try:
        stamp = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError as exc:
        raise PriceError(f"{label}: ogiltig tidsstämpel.") from exc
    if stamp.tzinfo is None:
        raise PriceError(f"{label}: tidsstämpeln saknar tidszon.")
    return stamp.timestamp()


def price_number(value, label="Pris"):
    if isinstance(value, bool):
        raise PriceError(f"{label}: ogiltigt pris.")
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise PriceError(f"{label}: ogiltigt pris.") from exc


def unit_factor(unit):
    """Multiplier from the sensor's unit to currency/kWh; öre and cent are divided by 100."""
    unit = (unit or "").strip()
    match = re.fullmatch(r"([^\s/]+)\s*/\s*(kWh|MWh)", unit, re.I)
    if not match:
        raise PriceError(f"Prisgivarens enhet ({unit or 'saknas'}) måste vara valuta/kWh eller valuta/MWh.")
    factor = 0.001 if match[2].lower() == "mwh" else 1
    if match[1].lower() in ("öre", "ore", "cent", "c", "snt"):
        factor /= 100
    return factor


def tibber_prices(response):
    try:
        homes = response["service_response"]["prices"]
    except (KeyError, TypeError) as exc:
        raise PriceError("Tibber returnerade inga priser.") from exc
    if not isinstance(homes, dict) or len(homes) != 1:
        raise PriceError("Tibber har flera hem eller inga hem; priser kan inte väljas entydigt.")
    rows = next(iter(homes.values()))
    if not isinstance(rows, list):
        raise PriceError("Tibber returnerade ogiltiga priser.")
    try:
        return sorted((parse_time(row["start_time"], "Tibber"), price_number(row["price"], "Tibber")) for row in rows)
    except (KeyError, TypeError) as exc:
        raise PriceError("Tibber returnerade ogiltiga priser.") from exc


def interval_prices(rows, value_key, factor, label):
    """[(start, price)] from rows with start, end and a price; hours are split to match quarter rows."""
    if not isinstance(rows, list) or len(rows) > 400:
        raise PriceError(f"{label} returnerade en ogiltig prislista.")
    intervals = []
    for row in rows:
        if not isinstance(row, dict):
            raise PriceError(f"{label} returnerade en ogiltig prisperiod.")
        start, end = parse_time(row.get("start"), label), parse_time(row.get("end"), label)
        if end - start not in (900, 3600):
            raise PriceError(f"{label}: prisperioder måste vara 15 eller 60 minuter.")
        if row.get(value_key) is None:
            continue
        intervals.append((start, end, price_number(row[value_key], label) * factor))
    intervals.sort()
    step = min((end - start for start, end, _ in intervals), default=900)
    return [(start + i * step, value) for start, end, value in intervals for i in range(int((end - start) // step))]


def hacs_prices(attrs):
    factor = unit_factor(attrs.get("unit_of_measurement")) if "/" in str(attrs.get("unit_of_measurement", "")) else 1
    rows = list(attrs.get("raw_today") or [])
    if attrs.get("tomorrow_valid") is True:
        rows += list(attrs.get("raw_tomorrow") or [])
    return interval_prices(rows, "value", factor, "Nord Pool")


def nordpool_area(entity_id, device_name, attrs):
    for text in (attrs.get("region"), attrs.get("area"), device_name, entity_id):
        match = AREA.search(str(text or "").replace(".", "_"))
        if match:
            return match[1].upper()
        if text and re.fullmatch(r"[A-Z]{2}\d?", str(text)):
            return str(text)
    raise PriceError("Nord Pool: hittar inte elområdet (t.ex. SE3) i givarens namn. Behåll elområdet i entitets-id:t.")


def load_prices(ha, entity_id, now, tz):
    """(prices, source) for the selected price sensor. prices = sorted [(start, kr/kWh)]."""
    state = ha.state(entity_id)
    attrs = state.get("attributes", {})
    if "raw_today" in attrs:
        return hacs_prices(attrs), "nordpool_hacs"
    source = ha.request("/template", {"template": SOURCE_TEMPLATE.replace("__ENTITY__", entity_id)})
    if isinstance(source, str):
        import json
        source = json.loads(source)
    platform = (source or {}).get("platform")
    local = datetime.fromtimestamp(now, tz)
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    if platform == "tibber":
        body = {"start": (midnight - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S"),
                "end": (midnight + timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")}
        return tibber_prices(ha.service("tibber", "get_prices", body, response=True)), "tibber"
    if platform == "nordpool":
        entry = source.get("entry")
        currency = re.match(r"([A-Z]{3})\s*/", attrs.get("unit_of_measurement", ""))
        if not entry or not currency:
            raise PriceError("Nord Pool: kan inte läsa konfiguration eller valuta. Välj givaren för aktuellt pris (valuta/kWh).")
        area = nordpool_area(entity_id, source.get("device"), attrs)
        rows = []
        for offset in (-1, 0, 1):
            day = (midnight + timedelta(days=offset)).date().isoformat()
            try:
                response = ha.service("nordpool", "get_prices_for_date", {
                    "config_entry": entry, "date": day, "areas": [area], "currency": currency[1]}, response=True)
                rows += response["service_response"][area]
            except Exception:  # yesterday may be gone and tomorrow is published around 13:00
                if offset == 0:
                    raise PriceError(f"Nord Pool saknar dagens priser för {area}.")
        # The action answers in currency per MWh (Home Assistant docs, nordpool.get_prices_for_date).
        return interval_prices(rows, "price", 0.001, "Nord Pool"), "nordpool"
    raise PriceError("Prisgivaren måste komma från Tibber eller Nord Pool.")
