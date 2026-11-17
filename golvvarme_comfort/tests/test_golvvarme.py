import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app  # noqa: E402
import prices  # noqa: E402
from planner import PlanError, build_plan, comfort_windows, effective, learn_rates, next_change, validate  # noqa: E402

TZ = ZoneInfo("Europe/Stockholm")
ENTITIES = {"climate_entity": "climate.golvvarme", "price_entity": "sensor.elpris",
            "conflict_automation": "automation.golvvarme_efter_elpris"}


def ts(text):
    return datetime.fromisoformat(text).replace(tzinfo=TZ).timestamp()


def flat_prices(day="2026-11-13", value=1.0, days=2, overrides=None):
    start = ts(f"{day}T00:00")
    prices = [(start + i * 900, value) for i in range(96 * days)]
    overrides = overrides or {}
    return [(t, overrides.get(t, v)) for t, v in prices]


def with_range(prices, a, b, value):
    return [(t, value if a <= t < b else v) for t, v in prices]


class PlannerTest(unittest.TestCase):
    def test_defaults_are_valid(self):
        config = validate({})
        self.assertFalse(config["enabled"])
        self.assertEqual(len(config["comfort_periods"]), 2)

    def test_validation_rejects_bad_order_and_times(self):
        with self.assertRaises(PlanError):
            validate({"economy_temp": 22})
        with self.assertRaises(PlanError):
            validate({"comfort_periods": [{"start": "25:00", "end": "06:00"}]})
        with self.assertRaises(PlanError):
            validate({"comfort_periods": [{"start": "05:00", "end": "06:00", "days": []}]})
        with self.assertRaises(PlanError):
            validate({"nope": 1})
        with self.assertRaises(PlanError):
            validate({"enabled": "yes"})

    def test_windows_cross_midnight_and_weekdays(self):
        config = validate({"comfort_periods": [{"start": "22:00", "end": "02:00", "days": [4]}]})  # fredag
        windows = comfort_windows(config, ts("2026-11-13T00:00"), ts("2026-11-15T00:00"), TZ)
        self.assertEqual(windows, [(ts("2026-11-13T22:00"), ts("2026-11-14T02:00"))])

    def test_windows_across_dst_change(self):
        config = validate({"comfort_periods": [{"start": "05:00", "end": "06:00"}]})
        windows = comfort_windows(config, ts("2026-10-24T12:00"), ts("2026-10-26T12:00"), TZ)
        self.assertIn((ts("2026-10-25T05:00"), ts("2026-10-25T06:00")), windows)
        self.assertEqual(windows[0][1] - windows[0][0], 3600)

    def test_flat_price_preheats_latest_possible(self):
        config = validate({})
        plan = build_plan(config, flat_prices(), ts("2026-11-13T12:00"), TZ, current_temp=18.0)
        # 21 - 18 = 3 °C at 1 °C/h -> 3 h before 16:30.
        self.assertEqual(plan["preheats"][0], {"start": ts("2026-11-13T13:30"), "comfort_start": ts("2026-11-13T16:30")})
        row = {r["t"]: r for r in plan["slots"]}
        self.assertEqual(row[ts("2026-11-13T13:15")]["target"], 17.0)
        self.assertEqual(row[ts("2026-11-13T13:30")]["target"], 21.0)
        self.assertTrue(row[ts("2026-11-13T13:30")]["reason"].startswith("Förvärmer inför 16:30"))
        self.assertEqual(row[ts("2026-11-13T21:00")]["target"], 17.0)

    def test_preheat_moves_earlier_into_cheap_hours(self):
        config = validate({"max_preheat_hours": 3, "heatup_rate": 2.0})  # needs 1.5 h from 18
        prices = flat_prices(value=2.0)
        prices = with_range(prices, ts("2026-11-13T13:30"), ts("2026-11-13T15:00"), 0.5)
        plan = build_plan(config, prices, ts("2026-11-13T12:00"), TZ, current_temp=18.0)
        self.assertEqual(plan["preheats"][0]["start"], ts("2026-11-13T13:30"))

    def test_preheat_does_not_move_earlier_when_holding_costs_more(self):
        config = validate({"max_preheat_hours": 3, "heatup_rate": 2.0, "holding_share": 1.0, "cooldown_rate": 0})
        prices = with_range(flat_prices(value=2.0), ts("2026-11-13T13:30"), ts("2026-11-13T14:00"), 1.9)
        plan = build_plan(config, prices, ts("2026-11-13T12:00"), TZ, current_temp=18.0)
        self.assertEqual(plan["preheats"][0]["start"], ts("2026-11-13T15:00"))

    def test_no_preheat_when_already_warm(self):
        plan = build_plan(validate({"cooldown_rate": 0}), flat_prices(), ts("2026-11-13T15:00"), TZ, current_temp=21.5)
        self.assertNotIn(ts("2026-11-13T16:30"), [p["comfort_start"] for p in plan["preheats"]])

    def test_cooling_floor_needs_preheat_later_in_the_day(self):
        # Warm at 08:00, but 8.5 h at 0.45 °C/h cools it to 17.7 °C by 16:30: 3.3 h of heating needed.
        plan = build_plan(validate({"max_preheat_hours": 5}), flat_prices(), ts("2026-11-13T08:00"), TZ, current_temp=21.5)
        self.assertEqual(plan["preheats"][0]["start"], ts("2026-11-13T13:15"))

    def test_preheat_aims_at_reduced_comfort_target(self):
        prices = with_range(flat_prices(value=1.2), ts("2026-11-13T16:30"), ts("2026-11-13T19:00"), 3.0)
        plan = build_plan(validate({"cooldown_rate": 0}), prices, ts("2026-11-13T12:00"), TZ, current_temp=17.0)
        row = {r["t"]: r for r in plan["slots"]}
        self.assertEqual(plan["preheats"][0]["start"], ts("2026-11-13T13:30"))
        self.assertEqual(row[ts("2026-11-13T14:00")]["target"], 20.0)

    def test_vacation_replaces_comfort_and_preheats_before_return(self):
        config = validate({"vacation": {"enabled": True, "start": "2026-11-13T10:00", "end": "2026-11-15T17:00", "temp": 12},
                           "cooldown_rate": 0})
        plan = build_plan(config, flat_prices(days=3), ts("2026-11-13T09:00"), TZ, current_temp=12.0, horizon_hours=72)
        row = {r["t"]: r for r in plan["slots"]}
        self.assertEqual(row[ts("2026-11-13T09:45")]["target"], 17.0)
        self.assertEqual(row[ts("2026-11-13T17:00")]["target"], 12.0)
        self.assertEqual(row[ts("2026-11-13T17:00")]["reason"], "Semester")
        self.assertEqual(row[ts("2026-11-14T05:30")]["target"], 12.0)
        # Home at 17:00 inside the 16:30-21:00 period: warm floor from then, preheated before.
        self.assertEqual(row[ts("2026-11-15T17:00")]["target"], 21.0)
        self.assertTrue(row[ts("2026-11-15T15:00")]["reason"].startswith("Förvärmer inför 17:00"))
        self.assertEqual(row[ts("2026-11-15T13:45")]["reason"], "Semester")

    def test_vacation_validation(self):
        with self.assertRaises(PlanError):
            validate({"vacation": {"enabled": True, "start": "2026-11-15T10:00", "end": "2026-11-14T10:00"}})
        with self.assertRaises(PlanError):
            validate({"vacation": {"enabled": True, "start": "igår", "end": "2026-11-14T10:00"}})
        with self.assertRaises(PlanError):
            validate({"vacation": {"enabled": False, "x": 1}})
        self.assertEqual(validate({"vacation": {"enabled": True, "start": "2026-11-13T10:00:30", "end": "2026-11-14T10:00"}})
                         ["vacation"]["start"], "2026-11-13T10:00")

    def test_expensive_relative_sets_back_outside_comfort_and_reduces_inside(self):
        config = validate({})
        prices = flat_prices(value=1.2)
        prices = with_range(prices, ts("2026-11-13T07:00"), ts("2026-11-13T09:00"), 3.0)
        prices = with_range(prices, ts("2026-11-13T17:00"), ts("2026-11-13T19:00"), 3.0)
        prices = with_range(prices, ts("2026-11-13T19:00"), ts("2026-11-13T21:00"), 2.5)
        plan = build_plan(config, prices, ts("2026-11-13T06:30"), TZ, current_temp=21.0)
        row = {r["t"]: r for r in plan["slots"]}
        self.assertEqual(row[ts("2026-11-13T07:00")]["target"], 16.0)
        self.assertEqual(row[ts("2026-11-13T07:00")]["reason"], "Dyr el – sänkt")
        self.assertEqual(row[ts("2026-11-13T17:00")]["target"], 20.0)
        self.assertEqual(row[ts("2026-11-13T10:00")]["target"], 17.0)

    def test_relative_never_expensive_below_minimum_price(self):
        prices = with_range(flat_prices(value=0.3), ts("2026-11-13T07:00"), ts("2026-11-13T12:00"), 0.8)
        plan = build_plan(validate({}), prices, ts("2026-11-13T06:30"), TZ, current_temp=21.0)
        self.assertFalse(any(r["expensive"] for r in plan["slots"]))

    def test_absolute_mode_and_cheap_boost(self):
        config = validate({"expensive_mode": "absolute", "expensive_price": 2.8, "cheap_boost_below": 0.4})
        prices = with_range(flat_prices(value=1.0), ts("2026-11-13T09:00"), ts("2026-11-13T10:00"), 3.0)
        prices = with_range(prices, ts("2026-11-13T11:00"), ts("2026-11-13T12:00"), 0.2)
        plan = build_plan(config, prices, ts("2026-11-13T08:00"), TZ, current_temp=21.0)
        row = {r["t"]: r for r in plan["slots"]}
        self.assertEqual(row[ts("2026-11-13T09:00")]["target"], 16.0)
        self.assertEqual(row[ts("2026-11-13T11:00")]["target"], 21.0)
        self.assertEqual(row[ts("2026-11-13T11:00")]["reason"], "Billig el – extra värme")

    def test_committed_preheat_is_kept(self):
        config = validate({})
        start = ts("2026-11-13T14:00")
        plan = build_plan(config, flat_prices(), ts("2026-11-13T15:00"), TZ, current_temp=20.9,
                          committed_preheat={ts("2026-11-13T16:30"): start})
        self.assertEqual(plan["slots"][0]["target"], 21.0)

    def test_missing_prices_fall_back_to_latest_start(self):
        prices = [p for p in flat_prices(days=1) if p[0] < ts("2026-11-13T12:00")]
        plan = build_plan(validate({}), prices, ts("2026-11-13T11:00"), TZ, current_temp=19.0)
        self.assertTrue(all(r["price"] is None for r in plan["slots"] if r["t"] >= ts("2026-11-13T12:00")))
        self.assertEqual(len(plan["slots"]), 24)  # minimum six hours even without future prices

    def test_hourly_prices_are_supported(self):
        start = ts("2026-11-13T00:00")
        prices = [(start + i * 3600, 1.0) for i in range(48)]
        plan = build_plan(validate({}), prices, ts("2026-11-13T12:10"), TZ, current_temp=18.0)
        self.assertTrue(all(r["price"] == 1.0 for r in plan["slots"]))
        self.assertEqual(plan["slots"][0]["t"], ts("2026-11-13T12:00"))

    def test_short_expensive_blips_are_ignored(self):
        prices = flat_prices(day="2026-11-12", value=1.2, days=3)
        prices = with_range(prices, ts("2026-11-13T00:00"), ts("2026-11-13T00:15"), 3.0)   # one quarter
        prices = with_range(prices, ts("2026-11-13T18:00"), ts("2026-11-13T18:30"), 3.0)   # inside comfort
        prices = with_range(prices, ts("2026-11-13T09:00"), ts("2026-11-13T10:00"), 3.0)   # a full hour
        plan = build_plan(validate({}), prices, ts("2026-11-12T23:30"), TZ, current_temp=18.0)
        row = {r["t"]: r for r in plan["slots"]}
        self.assertEqual((row[ts("2026-11-13T00:00")]["target"], row[ts("2026-11-13T00:00")]["expensive"]), (17.0, False))
        self.assertEqual(row[ts("2026-11-13T18:00")]["target"], 21.0)
        self.assertEqual(row[ts("2026-11-13T09:00")]["target"], 16.0)
        changes = [r["t"] for prev, r in zip(plan["slots"], plan["slots"][1:]) if r["target"] != prev["target"]]
        self.assertNotIn(ts("2026-11-13T18:00"), changes)

    def test_running_expensive_period_keeps_its_length_after_now(self):
        prices = with_range(flat_prices(value=1.2), ts("2026-11-13T09:00"), ts("2026-11-13T10:00"), 3.0)
        plan = build_plan(validate({}), prices, ts("2026-11-13T09:40"), TZ, current_temp=18.0)
        self.assertEqual((plan["slots"][0]["target"], plan["slots"][0]["expensive"]), (16.0, True))

    def test_expensive_run_at_end_of_known_prices_is_kept(self):
        prices = [p for p in flat_prices(value=1.2) if p[0] < ts("2026-11-14T00:00")]
        prices = with_range(prices, ts("2026-11-13T23:30"), ts("2026-11-14T00:00"), 3.0)
        plan = build_plan(validate({}), prices, ts("2026-11-13T20:00"), TZ, current_temp=18.0)
        row = {r["t"]: r for r in plan["slots"]}
        self.assertTrue(row[ts("2026-11-13T23:30")]["expensive"])

    def test_short_cheap_boost_is_ignored(self):
        config = validate({"cheap_boost_below": 0.4})
        prices = with_range(flat_prices(value=1.0), ts("2026-11-13T11:00"), ts("2026-11-13T11:30"), 0.2)
        plan = build_plan(config, prices, ts("2026-11-13T08:00"), TZ, current_temp=21.0)
        row = {r["t"]: r for r in plan["slots"]}
        self.assertEqual(row[ts("2026-11-13T11:00")]["target"], 17.0)

    def test_next_change(self):
        plan = build_plan(validate({}), flat_prices(), ts("2026-11-13T12:00"), TZ, current_temp=18.0)
        self.assertEqual(next_change(plan)["t"], ts("2026-11-13T13:30"))


class FakeHA:
    def __init__(self, climate=None, prices=None, platform="tibber", price_attrs=None):
        self.climate = climate or {"entity_id": "climate.golvvarme", "state": "auto", "attributes": {
            "current_temperature": 18.0, "temperature": 17, "hvac_action": "idle", "hvac_modes": ["heat", "auto"],
            "min_temp": 5, "max_temp": 35}}
        self.prices = prices if prices is not None else flat_prices()
        self.platform = platform
        self.price_attrs = price_attrs or {"unit_of_measurement": "SEK/kWh"}
        self.calls = []
        self.automation_on = False
        self.fail_prices = False
        self.store_step = None  # thermostat that rounds stored setpoints to this step
        self.history_rows = []

    def history(self, entity_id, start, end):
        return self.history_rows

    def state(self, entity_id):
        if entity_id.startswith("automation."):
            return {"state": "on" if self.automation_on else "off", "attributes": {"friendly_name": "Gammal"}}
        if entity_id.startswith("sensor."):
            return {"entity_id": entity_id, "state": "1.0", "attributes": self.price_attrs}
        return self.climate

    def time_zone(self):
        return "Europe/Stockholm"

    def service(self, domain, name, data, response=False):
        tz = ZoneInfo("Europe/Stockholm")
        if domain in ("tibber", "nordpool") and self.fail_prices:
            raise app.HaError("Priser nere")
        if domain == "tibber":
            return {"service_response": {"prices": {"Hem": [
                {"start_time": datetime.fromtimestamp(t, tz).isoformat(), "price": v} for t, v in self.prices]}}}
        if domain == "nordpool":
            self.calls.append((domain, name, data))
            day = data["date"]
            rows = [{"start": datetime.fromtimestamp(t, tz).isoformat(), "end": datetime.fromtimestamp(t + 900, tz).isoformat(),
                     "price": v * 1000} for t, v in self.prices if datetime.fromtimestamp(t, tz).date().isoformat() == day]
            if not rows:
                raise app.HaError("Inga priser")
            return {"service_response": {data["areas"][0]: rows}}
        self.calls.append((domain, name, data))
        if domain == "climate" and name == "set_temperature":
            stored = data["temperature"]
            if self.store_step:
                stored = int(stored / self.store_step) * self.store_step
            self.climate = {**self.climate, "state": data.get("hvac_mode", self.climate["state"]),
                            "attributes": {**self.climate["attributes"], "temperature": stored}}
        if domain == "climate" and name == "set_hvac_mode":
            self.climate = {**self.climate, "state": data["hvac_mode"]}
        return []

    def request(self, path, data=None):
        if path == "/template":
            return {"platform": self.platform, "entry": "ENTRY1", "device": "SE3"}
        self.calls.append(("request", path, data))
        return {}

def run(start, minutes, temps, heating, target):
    """Readings every 15 minutes from start, linearly between temps[0] and temps[1]."""
    steps = minutes // 15
    return [{"t": start + k * 900, "current": round(temps[0] + (temps[1] - temps[0]) * k / steps, 1),
             "target": target, "heating": heating} for k in range(steps + 1)]


class LearnedRatesTest(unittest.TestCase):
    def history(self):
        points, t = [], 1_800_000_000
        for _ in range(4):
            points += run(t, 120, (17.0, 19.4), True, 21)      # 1.2 °C/h warm-up
            t += 120 * 60 + 60
            points += run(t, 240, (19.4, 17.6), False, 16)     # 0.45 °C/h cool-down
            t += 240 * 60 + 60
            for k in range(4):                                  # holding: short on/off cycles, flat floor
                points += run(t, 15, (17.6, 17.6), k % 2 == 0, 17.5)
                t += 15 * 60 + 60
        return points

    def test_rates_are_medians_of_real_warm_ups_and_cool_downs(self):
        learned = learn_rates(self.history())
        self.assertEqual((learned["heatup_rate"], learned["cooldown_rate"]), (1.2, 0.45))
        self.assertEqual((learned["heat_runs"], learned["cool_runs"]), (4, 4))

    def test_short_or_few_runs_teach_nothing(self):
        few = learn_rates(self.history()[:30])
        self.assertIsNone(few["heatup_rate"])
        self.assertIsNone(learn_rates(run(1_800_000_000, 30, (17, 19), True, 21))["heatup_rate"])
        # Idle with the target above the floor is not a cool-down (the thermostat would heat).
        self.assertIsNone(learn_rates(run(1_800_000_000, 240, (19, 17), False, 21) * 3)["cooldown_rate"])

    def test_learned_rates_only_used_when_chosen(self):
        config = validate({})
        learned = {"heatup_rate": 1.5, "cooldown_rate": None}
        self.assertIs(effective(config, learned), config)
        on = effective({**config, "learn_rates": True}, learned)
        self.assertEqual((on["heatup_rate"], on["cooldown_rate"]), (1.5, config["cooldown_rate"]))
        with self.assertRaises(PlanError):
            validate({"learn_rates": "ja"})


class ControllerTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.ha = FakeHA()
        self.controller = app.Controller(self.ha, self.dir.name)
        self.controller.save_settings(ENTITIES)

    def tearDown(self):
        self.dir.cleanup()

    def writes(self):
        return [c for c in self.ha.calls if c[0] == "climate"]

    def test_learned_rates_reach_the_status_and_the_plan(self):
        self.ha.history_rows = [{"last_changed": datetime.fromtimestamp(p["t"]).astimezone().isoformat(),
                                 "attributes": {"current_temperature": p["current"], "temperature": p["target"],
                                                "hvac_action": "heating" if p["heating"] else "idle"}}
                                for p in LearnedRatesTest().history()]
        self.controller.tick(ts("2026-11-13T14:00"))
        self.assertEqual(self.controller.status["learned_rates"]["heatup_rate"], 1.2)
        self.assertFalse(self.controller.status["learned_rates"]["in_use"])
        self.controller.save_settings({"learn_rates": True})
        self.controller.tick(ts("2026-11-13T14:01"))
        self.assertTrue(self.controller.status["learned_rates"]["in_use"])
        # Read again only after the refresh interval.
        self.ha.history_rows = []
        self.controller.tick(ts("2026-11-13T15:00"))
        self.assertEqual(self.controller.status["learned_rates"]["heatup_rate"], 1.2)

    def test_dry_run_never_writes(self):
        self.controller.tick(ts("2026-11-13T14:00"))
        self.assertEqual(self.writes(), [])
        self.assertEqual(self.controller.status["target"], 21.0)
        self.assertIn("bara plan", self.controller.status["message"])

    def test_enabled_writes_target_once_then_follows(self):
        self.controller.save_settings({"enabled": True})
        self.controller.tick(ts("2026-11-13T14:00"))
        self.assertEqual(self.writes(), [("climate", "set_temperature",
                                          {"entity_id": "climate.golvvarme", "hvac_mode": "heat", "temperature": 21.0})])
        self.controller.tick(ts("2026-11-13T14:01"))
        self.assertEqual(len(self.writes()), 1)
        self.assertEqual(self.controller.status["message"], "Termostaten följer planen")

    def test_rate_limit_between_writes(self):
        self.controller.save_settings({"enabled": True})
        self.controller.tick(ts("2026-11-13T21:01"))
        self.ha.climate["attributes"]["temperature"] = 25  # someone changed it by hand
        self.controller.tick(ts("2026-11-13T21:03"))
        self.assertEqual(len(self.writes()), 1)
        self.controller.tick(ts("2026-11-13T21:07"))
        self.assertEqual(len(self.writes()), 2)

    def test_disabling_hands_back_to_program(self):
        self.controller.save_settings({"enabled": True})
        self.controller.tick(ts("2026-11-13T14:00"))
        self.controller.save_settings({"enabled": False})
        self.assertEqual(self.writes()[-1], ("climate", "set_hvac_mode", {"entity_id": "climate.golvvarme", "hvac_mode": "auto"}))

    def test_missing_prices_hand_back(self):
        self.controller.save_settings({"enabled": True})
        self.controller.tick(ts("2026-11-13T14:00"))
        self.ha.fail_prices = True
        self.controller.prices = []
        self.controller.tick(ts("2026-11-13T14:10"))
        self.assertEqual(self.writes()[-1][1], "set_hvac_mode")
        self.assertIn("Inga elpriser", self.controller.status["message"])

    def test_unavailable_thermostat_is_not_written(self):
        self.controller.save_settings({"enabled": True})
        self.ha.climate = {"state": "unavailable", "attributes": {}}
        self.controller.tick(ts("2026-11-13T14:00"))
        self.assertEqual(self.writes(), [])

    def test_thermostat_that_ignores_writes_backs_off(self):
        self.controller.save_settings({"enabled": True})
        real = self.ha.service

        def ignoring(domain, name, data, response=False):
            if domain != "climate":
                return real(domain, name, data, response)
            self.ha.calls.append((domain, name, data))  # accepted by HA, never stored by the thermostat
            return []
        self.ha.service = ignoring
        start = ts("2026-11-13T14:00")
        for minute in range(0, 60):
            self.controller.tick(start + minute * 60)
        # three tries five minutes apart (0, 5, 10), then only every 30 minutes (40)
        self.assertEqual(len(self.writes()), 4)
        self.assertIn("inte tagit emot 21,0", self.controller.status["error"])
        errors = [e for e in self.controller.events.rows if e["kind"] == "error"]
        self.assertEqual(len(errors), 1)
        self.ha.climate = {**self.ha.climate, "state": "heat",
                           "attributes": {**self.ha.climate["attributes"], "temperature": 21.0}}
        self.controller.tick(start + 3600)
        self.assertIsNone(self.controller.unconfirmed)
        self.assertNotIn("error", self.controller.status)

    def test_unreadable_thermostat_is_published(self):
        self.controller.tick(ts("2026-11-13T14:00"))
        self.ha.state = lambda entity_id: (_ for _ in ()).throw(app.HaError("nere"))
        self.ha.calls.clear()
        self.controller.tick(ts("2026-11-13T14:01"))
        published = {c[1]: c[2]["state"] for c in self.ha.calls if c[0] == "request"}
        self.assertEqual(published["/states/sensor.golvvarme_comfort_status"], "Väntar")

    def test_events_use_decimal_comma(self):
        self.controller.save_settings({"enabled": True})
        self.controller.tick(ts("2026-11-13T14:00"))
        self.assertTrue(self.controller.events.recent(1)[0]["message"].startswith("21,0 °C"))

    def test_too_large_request_is_rejected(self):
        import threading
        from http.server import ThreadingHTTPServer
        from urllib.error import HTTPError
        from urllib.request import Request, urlopen
        server = ThreadingHTTPServer(("127.0.0.1", 0), app.handler_for(self.controller, open_access=True))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        body = json.dumps({"comfort_temp": 22.0, "x": "a" * 20000}).encode()
        request = Request(f"http://127.0.0.1:{server.server_port}/api/settings", data=body, method="POST",
                          headers={"Content-Type": "application/json"})
        with self.assertRaises(HTTPError) as caught:
            urlopen(request, timeout=3)
        self.assertEqual(caught.exception.code, 413)
        caught.exception.close()
        self.assertEqual(self.controller.config["comfort_temp"], 21.0)

    def test_conflict_reported(self):
        self.ha.automation_on = True
        self.controller.tick(ts("2026-11-13T14:00"))
        self.assertEqual(self.controller.status["conflict"]["entity_id"], "automation.golvvarme_efter_elpris")

    def test_preheat_committed_while_room_warms(self):
        self.controller.save_settings({"enabled": True})
        self.controller.tick(ts("2026-11-13T14:00"))
        self.ha.climate["attributes"]["current_temperature"] = 20.8  # would now need only 0.2 h
        self.controller.tick(ts("2026-11-13T14:30"))
        self.assertEqual(self.controller.status["target"], 21.0)

    def test_settings_persist(self):
        self.controller.save_settings({"comfort_temp": 22.0})
        again = app.Controller(self.ha, self.dir.name)
        self.assertEqual(again.config["comfort_temp"], 22.0)
        self.assertEqual(json.loads((Path(self.dir.name) / "settings.json").read_text())["comfort_temp"], 22.0)

    def test_heating_summary(self):
        rows = [{"last_changed": "2026-11-13T10:00:00+01:00", "attributes": {"hvac_action": "heating"}},
                {"last_changed": "2026-11-13T11:30:00+01:00", "attributes": {"hvac_action": "idle"}}]
        result = app.heating_summary(rows, flat_prices(value=2.0), 1.0, ts("2026-11-13T00:00"), ts("2026-11-13T12:00"))
        self.assertEqual(result, {"hours": 1.5, "kwh": 1.5, "cost": 3.0})

    def test_thermostat_without_auto_gets_fallback_temperature(self):
        self.ha.climate["attributes"]["hvac_modes"] = ["heat", "off"]
        self.controller.save_settings({"enabled": True, "fallback_temp": 19.0})
        self.controller.tick(ts("2026-11-13T14:00"))
        self.controller.save_settings({"enabled": False})
        self.assertEqual(self.writes()[-1], ("climate", "set_temperature",
                                             {"entity_id": "climate.golvvarme", "temperature": 19.0, "hvac_mode": "heat"}))
        self.assertIn("fast 19,0", self.controller.fallback(app.thermostat_caps(self.ha.climate["attributes"])))

    def test_thermostat_without_heat_mode_gets_only_temperature(self):
        self.ha.climate = {"state": "auto", "attributes": {"hvac_modes": ["auto", "off"], "temperature": 17,
                                                            "current_temperature": 18}}
        self.controller.save_settings({"enabled": True})
        self.controller.tick(ts("2026-11-13T14:00"))
        self.assertEqual(self.writes(), [("climate", "set_temperature", {"entity_id": "climate.golvvarme", "temperature": 21.0})])
        self.controller.tick(ts("2026-11-13T14:10"))
        self.assertEqual(len(self.writes()), 1)

    def test_setpoint_respects_step_and_limits(self):
        caps = app.thermostat_caps({"target_temp_step": 1, "min_temp": 18, "max_temp": 20})
        self.assertEqual(app.setpoint(caps, 20.5), 20)
        self.assertEqual(app.setpoint(caps, 16.0), 18)
        caps = app.thermostat_caps({"target_temp_step": 1})
        self.assertEqual(app.setpoint(caps, 20.5), 21)
        self.assertEqual(app.setpoint(caps, 21.5), 22)
        self.assertEqual(app.setpoint(app.thermostat_caps({}), 20.3), 20.5)

    def test_thermostat_storing_other_step_does_not_loop(self):
        self.ha.store_step = 1  # advertises nothing but stores whole degrees
        self.controller.save_settings({"enabled": True, "comfort_reduction": 0, "comfort_temp": 20.5})
        self.controller.tick(ts("2026-11-13T17:00"))
        self.assertEqual(self.ha.climate["attributes"]["temperature"], 20)
        for minute in (6, 12, 18):
            self.controller.tick(ts(f"2026-11-13T17:{minute:02d}"))
        self.assertEqual(len(self.writes()), 1)
        self.assertEqual(self.controller.status["message"], "Termostaten följer planen")

    def test_official_nordpool_prices(self):
        self.ha.platform = "nordpool"
        self.controller.tick(ts("2026-11-13T14:00"))
        self.assertEqual(self.controller.status["price"], 1.0)
        self.assertEqual(self.controller.status["price_source"], "Nord Pool (spotpris)")
        calls = [c for c in self.ha.calls if c[0] == "nordpool"]
        self.assertEqual(calls[0][2]["areas"], ["SE3"])
        self.assertEqual(calls[0][2]["currency"], "SEK")

    def test_unknown_price_platform_is_reported(self):
        self.ha.platform = ""
        self.controller.tick(ts("2026-11-13T14:00"))
        self.assertIn("Tibber eller Nord Pool", self.controller.status["price_error"])


    def test_vacation_ends_by_itself(self):
        self.controller.save_settings({"vacation": {"enabled": True, "start": "2026-11-10T10:00", "end": "2026-11-13T12:00",
                                                    "temp": 12}})
        self.controller.tick(ts("2026-11-13T11:00"))
        self.assertTrue(self.controller.status["vacation"]["active"])
        self.assertEqual(self.controller.status["target"], 12.0)
        self.controller.tick(ts("2026-11-13T12:01"))
        self.assertFalse(self.controller.config["vacation"]["enabled"])
        self.assertFalse(self.controller.status["vacation"]["active"])
        self.assertIn("Semestern är slut", self.controller.events.rows[-1]["message"])

    def test_new_install_asks_for_entities(self):
        with tempfile.TemporaryDirectory() as folder:
            ha = FakeHA()
            controller = app.Controller(ha, folder)
            controller.tick(ts("2026-11-13T12:00"))
            self.assertTrue(controller.status["setup"])
            self.assertEqual([c for c in ha.calls if c[0] == "climate"], [])


class PricesTest(unittest.TestCase):
    def test_hacs_nordpool_attributes(self):
        tz = ZoneInfo("Europe/Stockholm")
        start = ts("2026-11-13T00:00")
        hour = lambda i, v: {"start": datetime.fromtimestamp(start + i * 3600, tz).isoformat(),
                             "end": datetime.fromtimestamp(start + (i + 1) * 3600, tz).isoformat(), "value": v}
        attrs = {"unit_of_measurement": "öre/kWh", "raw_today": [hour(i, 150) for i in range(24)],
                 "raw_tomorrow": [hour(24 + i, 90) for i in range(24)], "tomorrow_valid": False}
        result = prices.hacs_prices(attrs)
        self.assertEqual(len(result), 24)
        self.assertEqual(result[0], (start, 1.5))
        attrs["tomorrow_valid"] = True
        self.assertEqual(prices.hacs_prices(attrs)[-1], (start + 47 * 3600, 0.9))

    def test_hacs_nordpool_through_controller(self):
        tz = ZoneInfo("Europe/Stockholm")
        start = ts("2026-11-13T00:00")
        rows = [{"start": datetime.fromtimestamp(start + i * 900, tz).isoformat(),
                 "end": datetime.fromtimestamp(start + (i + 1) * 900, tz).isoformat(), "value": 0.8} for i in range(96)]
        with tempfile.TemporaryDirectory() as folder:
            ha = FakeHA(platform="nordpool", price_attrs={"unit_of_measurement": "SEK/kWh", "raw_today": rows})
            controller = app.Controller(ha, folder)
            controller.save_settings(ENTITIES)
            controller.tick(ts("2026-11-13T14:00"))
            self.assertEqual(controller.status["price"], 0.8)
            self.assertEqual(controller.status["price_source"], "Nord Pool (HACS)")

    def test_units_and_areas(self):
        self.assertEqual(prices.unit_factor("SEK/kWh"), 1)
        self.assertEqual(prices.unit_factor("EUR/MWh"), 0.001)
        self.assertEqual(prices.unit_factor("öre/kWh"), 0.01)
        with self.assertRaises(prices.PriceError):
            prices.unit_factor("kr")
        self.assertEqual(prices.nordpool_area("sensor.nord_pool_se4_current_price", None, {}), "SE4")
        self.assertEqual(prices.nordpool_area("sensor.current_price", "SE3", {}), "SE3")
        with self.assertRaises(prices.PriceError):
            prices.nordpool_area("sensor.current_price", "Nord Pool", {})

    def test_gaps_and_bad_rows_rejected(self):
        with self.assertRaises(prices.PriceError):
            prices.interval_prices([{"start": "2026-11-13T00:00:00", "end": "2026-11-13T01:00:00", "value": 1}], "value", 1, "X")
        with self.assertRaises(prices.PriceError):
            prices.tibber_prices({"service_response": {"prices": {"A": [], "B": []}}})


if __name__ == "__main__":
    unittest.main()
