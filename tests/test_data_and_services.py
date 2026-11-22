import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from busmanager import actions, engine, rules, seed
from busmanager.db import connect, init_db

MONDAY = date(2026, 11, 2)


def fresh_game(test, **kwargs):
    kwargs.setdefault("start_date", MONDAY)
    conn = connect(":memory:")
    test.addCleanup(conn.close)
    init_db(conn)
    return conn, seed.new_game(conn, **kwargs)


def line_id(conn, code):
    return conn.execute("SELECT id FROM line WHERE code = ?", (code,)).fetchone()["id"]


class ServiceDayTypeTests(unittest.TestCase):
    def test_weekday_and_weekend_services_can_share_a_slot(self):
        conn, cid = fresh_game(self)
        l3 = line_id(conn, "3")
        d = actions.hire_driver(conn, cid, "Test")
        a = actions.create_service(conn, l3, 6, 14, "weekday")
        b = actions.create_service(conn, l3, 6, 14, "weekend")
        actions.assign_service(conn, a, driver_id=d)
        actions.assign_service(conn, b, driver_id=d)

    def test_saturday_and_sunday_do_not_conflict_but_weekend_conflicts_with_both(self):
        conn, cid = fresh_game(self)
        l3 = line_id(conn, "3")
        d = actions.hire_driver(conn, cid, "Test")
        sat = actions.create_service(conn, l3, 6, 14, "saturday")
        sun = actions.create_service(conn, l3, 6, 14, "sunday")
        both = actions.create_service(conn, l3, 6, 14, "weekend")
        actions.assign_service(conn, sat, driver_id=d)
        actions.assign_service(conn, sun, driver_id=d)
        with self.assertRaises(actions.GameError):
            actions.assign_service(conn, both, driver_id=d)

    def test_all_conflicts_with_weekday(self):
        conn, cid = fresh_game(self)
        l3 = line_id(conn, "3")
        d = actions.hire_driver(conn, cid, "Test")
        a = actions.create_service(conn, l3, 6, 14, "weekday")
        b = actions.create_service(conn, l3, 6, 14, "all")
        actions.assign_service(conn, a, driver_id=d)
        with self.assertRaises(actions.GameError):
            actions.assign_service(conn, b, driver_id=d)

    def test_weekday_only_service_does_not_run_on_weekend(self):
        conn, cid = fresh_game(self)
        conn.execute("UPDATE service SET day_type = 'weekday'")
        results = engine.catch_up(conn, cid, 7)
        self.assertGreater(results[0]["served"], 0)
        self.assertEqual(results[5]["served"], 0)
        self.assertEqual(results[6]["served"], 0)

    def test_sunday_only_service_runs_only_on_sunday(self):
        conn, cid = fresh_game(self)
        conn.execute("UPDATE service SET day_type = 'sunday'")
        results = engine.catch_up(conn, cid, 7)
        self.assertEqual([r["served"] > 0 for r in results], [False] * 6 + [True])

    def test_invalid_day_type_rejected(self):
        conn, cid = fresh_game(self)
        with self.assertRaises(actions.GameError):
            actions.create_service(conn, line_id(conn, "1"), 6, 10, "lundi")

    def test_unassign_and_delete(self):
        conn, cid = fresh_game(self)
        sid = conn.execute("SELECT id FROM service LIMIT 1").fetchone()["id"]
        actions.unassign_service(conn, sid, vehicle=True, driver=True)
        row = conn.execute("SELECT vehicle_id, driver_id FROM service WHERE id = ?", (sid,)).fetchone()
        self.assertIsNone(row["vehicle_id"])
        self.assertIsNone(row["driver_id"])
        actions.delete_service(conn, sid)
        with self.assertRaises(actions.GameError):
            actions.delete_service(conn, sid)


class WorkshopTests(unittest.TestCase):
    def test_overhaul_and_refurbish(self):
        conn, cid = fresh_game(self)
        vid = conn.execute("SELECT id FROM vehicle ORDER BY id LIMIT 1").fetchone()["id"]
        conn.execute("UPDATE vehicle SET engine_state = 20, interior_state = 10 WHERE id = ?", (vid,))
        actions.overhaul_engine(conn, vid)
        v = conn.execute("SELECT * FROM vehicle WHERE id = ?", (vid,)).fetchone()
        self.assertEqual(v["engine_state"], 100)
        self.assertEqual(v["status"], "workshop")
        self.assertEqual(v["available_from_day"], rules.OVERHAUL_DOWNTIME_DAYS)
        with self.assertRaises(actions.GameError):
            actions.refurbish_interior(conn, vid)
        conn.execute("UPDATE vehicle SET status = 'active'")
        actions.refurbish_interior(conn, vid)
        self.assertEqual(conn.execute("SELECT interior_state FROM vehicle WHERE id = ?", (vid,)).fetchone()[0], 100)
        total = conn.execute("SELECT SUM(amount) FROM ledger").fetchone()[0]
        self.assertAlmostEqual(conn.execute("SELECT cash FROM company").fetchone()[0], total, places=4)

    def test_bus_is_immobilised_exactly_the_announced_duration(self):
        conn, cid = fresh_game(self)
        sid = conn.execute("SELECT id, vehicle_id FROM service WHERE start_hour = 5 AND line_id = ?",
                           (line_id(conn, "1"),)).fetchone()
        vid = sid["vehicle_id"]
        conn.execute("UPDATE service SET driver_id = NULL WHERE vehicle_id != ?", (vid,))  # isole ce bus
        conn.execute("UPDATE vehicle SET revision_due_day = 999, ct_due_day = 999")
        conn.execute("UPDATE bus_model SET reliability = 1")
        actions.refurbish_interior(conn, vid)
        results = engine.catch_up(conn, cid, rules.REFURBISH_DOWNTIME_DAYS + 2)
        ran = [r["served"] > 0 for r in results]
        self.assertEqual(ran, [False] * rules.REFURBISH_DOWNTIME_DAYS + [True, True])


class CalendarTests(unittest.TestCase):
    def test_day_type_follows_real_calendar(self):
        sunday = date(2026, 11, 22)
        self.assertEqual(rules.day_type(0, sunday), "sunday")
        self.assertEqual(rules.day_type(1, sunday), "weekday")
        self.assertEqual(rules.day_type(6, sunday), "saturday")
        self.assertEqual(rules.day_type(5, MONDAY), "saturday")
        self.assertEqual(rules.day_type(6, MONDAY), "sunday")
        self.assertEqual(rules.day_type(4, MONDAY), "weekday")

    def test_engine_uses_company_start_date(self):
        conn, cid = fresh_game(self, start_date=date(2026, 11, 22))
        sunday, monday = engine.catch_up(conn, cid, 2)
        self.assertEqual((sunday["day_type"], monday["day_type"]), ("sunday", "weekday"))
        self.assertLess(sunday["demand"], monday["demand"])

    def test_target_day_for_date(self):
        conn, cid = fresh_game(self, start_date=date(2026, 11, 1))
        self.assertEqual(engine.target_day_for_date(conn, cid, date(2026, 11, 1)), 1)
        self.assertEqual(engine.target_day_for_date(conn, cid, date(2026, 11, 10)), 10)

    def test_cron_late_or_twice_is_safe(self):
        conn, cid = fresh_game(self, start_date=date(2026, 11, 1))
        target = engine.target_day_for_date(conn, cid, date(2026, 11, 5))
        self.assertEqual(len(engine.catch_up(conn, cid, target)), 5)
        self.assertEqual(engine.catch_up(conn, cid, target), [])

    def test_catch_up_all_handles_every_company(self):
        conn, c1 = fresh_game(self, start_date=date(2026, 11, 1))
        c2 = seed.new_game(conn, start_date=date(2026, 11, 3))
        done = engine.catch_up_all(conn, date(2026, 11, 5))
        self.assertEqual((len(done[c1]), len(done[c2])), (5, 3))


class BandTests(unittest.TestCase):
    def test_bands_and_departures(self):
        self.assertEqual(rules.band_of("weekday", 7), "pointe")
        self.assertEqual(rules.band_of("weekday", 12), "creuse")
        self.assertEqual(rules.band_of("weekday", 20), "soir")
        self.assertEqual(rules.band_of("sunday", 7), "creuse")
        self.assertEqual(rules.departures_per_bus_hour(45), 30 / 45)


class DataLoadingTests(unittest.TestCase):
    def _data_dir(self, models=None, lines=None, game=None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(seed.DATA_DIR)
        for name, override in (("bus_models", models), ("lines", lines), ("options", None), ("game", game)):
            data = override if override is not None else json.loads((base / f"{name}.json").read_text())
            (Path(tmp.name) / f"{name}.json").write_text(json.dumps(data))
        return tmp.name

    def _line(self, **over):
        freq = {d: {"creuse": 1, "pointe": 2, "soir": 1} for d in rules.DAY_TYPES}
        dem = {d: {"creuse": 50, "pointe": 200, "soir": 30} for d in rules.DAY_TYPES}
        line = {"code": "X1", "name": "Test", "distance_km": 5, "duration_min": 20, "fare": 1.0,
                "allowed_bodies": ["standard"], "frequency": freq, "demand_per_hour": dem}
        line.update(over)
        return line

    def test_custom_lines_with_explicit_hourly_demand(self):
        line = self._line(hourly={"weekday": {"8": 300, "17": 250}, "saturday": {"11": 100}, "sunday": {}})
        del line["demand_per_hour"]
        game = {"start_cash": 100000, "start_fleet": []}
        conn, cid = fresh_game(self, data_dir=self._data_dir(lines=[line], game=game))
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM line").fetchone()[0], 1)
        self.assertEqual(conn.execute(
            "SELECT passengers FROM line_demand WHERE day_type='weekday' AND hour=8").fetchone()[0], 300)

    def test_demand_per_band_is_spread_over_band_hours(self):
        game = {"start_cash": 100000, "start_fleet": []}
        conn, cid = fresh_game(self, data_dir=self._data_dir(lines=[self._line()], game=game))
        rows = {r["hour"]: r["passengers"] for r in conn.execute(
            "SELECT hour, passengers FROM line_demand WHERE day_type = 'weekday'")}
        self.assertEqual(rows[7], 200)   # pointe
        self.assertEqual(rows[12], 50)   # creuse
        self.assertEqual(rows[20], 30)   # soir

    def test_missing_field_is_reported(self):
        line = self._line()
        del line["duration_min"]
        conn = connect(":memory:")
        self.addCleanup(conn.close)
        init_db(conn)
        with self.assertRaises(seed.DataError) as ctx:
            seed.new_game(conn, data_dir=self._data_dir(lines=[line], game={"start_fleet": []}))
        self.assertIn("duration_min", str(ctx.exception))

    def test_missing_frequency_for_a_day_type_is_reported(self):
        line = self._line()
        del line["frequency"]["sunday"]
        conn = connect(":memory:")
        self.addCleanup(conn.close)
        init_db(conn)
        with self.assertRaises(seed.DataError) as ctx:
            seed.new_game(conn, data_dir=self._data_dir(lines=[line], game={"start_fleet": []}))
        self.assertIn("sunday", str(ctx.exception))

    def test_invalid_energy_is_reported(self):
        models = [{"brand": "X", "model": "Y", "body": "standard", "status": "neuf", "capacity": 80,
                   "motors": [{"energy": "hydrogene", "consumption_per_100km": 10, "price": 1}]}]
        conn = connect(":memory:")
        self.addCleanup(conn.close)
        init_db(conn)
        with self.assertRaises(seed.DataError) as ctx:
            seed.new_game(conn, data_dir=self._data_dir(models=models, game={"start_fleet": []}))
        self.assertIn("hydrogene", str(ctx.exception))

    def test_electric_requires_range(self):
        models = [{"brand": "X", "model": "Y", "body": "standard", "status": "neuf", "capacity": 80,
                   "motors": [{"energy": "electrique", "consumption_per_100km": 100, "price": 1}]}]
        conn = connect(":memory:")
        self.addCleanup(conn.close)
        init_db(conn)
        with self.assertRaises(seed.DataError) as ctx:
            seed.new_game(conn, data_dir=self._data_dir(models=models, game={"start_fleet": []}))
        self.assertIn("range_km", str(ctx.exception))

    def test_unknown_start_fleet_model_is_reported(self):
        conn = connect(":memory:")
        self.addCleanup(conn.close)
        init_db(conn)
        game = {"start_fleet": [{"model": "N'existe pas", "line": "1"}]}
        with self.assertRaises(seed.DataError):
            seed.new_game(conn, data_dir=self._data_dir(game=game))

    def test_default_catalogue(self):
        conn, cid = fresh_game(self)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM bus_model").fetchone()[0], 44)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM bus_model WHERE energy='electrique'").fetchone()[0], 9)
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM bus_model WHERE model = 'GX127' AND energy='gnv'").fetchone()[0], 0)  # pas de GNV
        self.assertTrue(all(r[0] for r in conn.execute("SELECT range_km FROM bus_model WHERE energy='electrique'")))


if __name__ == "__main__":
    unittest.main()
