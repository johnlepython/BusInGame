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


class ServiceDayTypeTests(unittest.TestCase):
    def test_weekday_and_weekend_services_can_share_a_slot(self):
        conn, cid = fresh_game(self)
        l3 = conn.execute("SELECT id FROM line WHERE code = 'L3'").fetchone()["id"]
        d = actions.hire_driver(conn, cid, "Test")
        a = actions.create_service(conn, l3, 6, 14, "weekday")
        b = actions.create_service(conn, l3, 6, 14, "weekend")
        actions.assign_service(conn, a, driver_id=d)
        actions.assign_service(conn, b, driver_id=d)  # pas de conflit : jamais le même jour

    def test_all_conflicts_with_weekday(self):
        conn, cid = fresh_game(self)
        l3 = conn.execute("SELECT id FROM line WHERE code = 'L3'").fetchone()["id"]
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
        self.assertGreater(results[0]["served"], 0)   # jour 0 = semaine
        self.assertEqual(results[5]["served"], 0)     # jour 5 = week-end
        self.assertEqual(results[6]["served"], 0)

    def test_invalid_day_type_rejected(self):
        conn, cid = fresh_game(self)
        l1 = conn.execute("SELECT id FROM line WHERE code = 'L1'").fetchone()["id"]
        with self.assertRaises(actions.GameError):
            actions.create_service(conn, l1, 6, 10, "dimanche")

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
        vid = conn.execute("SELECT id FROM vehicle").fetchone()["id"]
        conn.execute("UPDATE vehicle SET engine_state = 20, interior_state = 10")
        actions.overhaul_engine(conn, vid)
        v = conn.execute("SELECT * FROM vehicle WHERE id = ?", (vid,)).fetchone()
        self.assertEqual(v["engine_state"], 100)
        self.assertEqual(v["status"], "workshop")
        self.assertEqual(v["available_from_day"], rules.OVERHAUL_DOWNTIME_DAYS + 1)
        with self.assertRaises(actions.GameError):  # déjà à l'atelier
            actions.refurbish_interior(conn, vid)
        conn.execute("UPDATE vehicle SET status = 'active'")
        actions.refurbish_interior(conn, vid)
        self.assertEqual(conn.execute("SELECT interior_state FROM vehicle").fetchone()[0], 100)
        total = conn.execute("SELECT SUM(amount) FROM ledger").fetchone()[0]
        self.assertAlmostEqual(conn.execute("SELECT cash FROM company").fetchone()[0], total, places=4)

    def test_bus_returns_from_workshop_after_downtime(self):
        conn, cid = fresh_game(self)
        vid = conn.execute("SELECT id FROM vehicle").fetchone()["id"]
        actions.refurbish_interior(conn, vid)  # indisponible jours 0..2, de retour au jour 3
        results = engine.catch_up(conn, cid, 4)
        self.assertEqual([r["served"] > 0 for r in results], [False, False, False, True])


class CalendarTests(unittest.TestCase):
    def test_day_type_follows_real_calendar(self):
        sunday = date(2026, 11, 22)
        self.assertEqual(rules.day_type(0, sunday), "weekend")
        self.assertEqual(rules.day_type(1, sunday), "weekday")   # lundi
        self.assertEqual(rules.day_type(6, sunday), "weekend")   # samedi suivant
        self.assertEqual(rules.day_type(5, MONDAY), "weekend")
        self.assertEqual(rules.day_type(4, MONDAY), "weekday")

    def test_engine_uses_company_start_date(self):
        conn, cid = fresh_game(self, start_date=date(2026, 11, 22))  # un dimanche
        weekend_day, weekday = engine.catch_up(conn, cid, 2)
        self.assertLess(weekend_day["demand"], weekday["demand"])

    def test_target_day_for_date(self):
        conn, cid = fresh_game(self, start_date=date(2026, 11, 1))
        self.assertEqual(engine.target_day_for_date(conn, cid, date(2026, 11, 1)), 1)   # jour 0 calculé le soir même
        self.assertEqual(engine.target_day_for_date(conn, cid, date(2026, 11, 10)), 10)

    def test_cron_late_or_twice_is_safe(self):
        conn, cid = fresh_game(self, start_date=date(2026, 11, 1))
        target = engine.target_day_for_date(conn, cid, date(2026, 11, 5))
        self.assertEqual(len(engine.catch_up(conn, cid, target)), 5)  # 5 jours rattrapés d'un coup
        self.assertEqual(engine.catch_up(conn, cid, target), [])      # relance : rien de plus


class DataLoadingTests(unittest.TestCase):
    def _data_dir(self, models=None, lines=None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(seed.DATA_DIR)
        (Path(tmp.name) / "bus_models.json").write_text(
            json.dumps(models if models is not None else json.loads((base / "bus_models.json").read_text())))
        (Path(tmp.name) / "lines.json").write_text(
            json.dumps(lines if lines is not None else json.loads((base / "lines.json").read_text())))
        return tmp.name

    def test_custom_lines_with_explicit_hourly_demand(self):
        lines = [{"code": "X1", "name": "Test", "distance_km": 5, "duration_min": 20, "fare": 1.0,
                  "hourly": {"weekday": {"8": 300, "17": 250}, "weekend": {"11": 100}}}]
        conn, cid = fresh_game(self, data_dir=self._data_dir(lines=lines))
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM line").fetchone()[0], 1)
        row = conn.execute("SELECT passengers FROM line_demand WHERE day_type='weekday' AND hour=8").fetchone()
        self.assertEqual(row[0], 300)

    def test_missing_field_is_reported(self):
        lines = [{"code": "X1", "name": "Test", "distance_km": 5, "fare": 1.0, "weekday_passengers": 100}]
        conn = connect(":memory:")
        self.addCleanup(conn.close)
        init_db(conn)
        with self.assertRaises(seed.DataError) as ctx:
            seed.new_game(conn, data_dir=self._data_dir(lines=lines))
        self.assertIn("duration_min", str(ctx.exception))

    def test_invalid_model_is_reported(self):
        models = [{"name": "Bus", "energy": "hydrogene", "body": "standard", "capacity": 80,
                   "consumption_per_100km": 10, "price": 1, "reliability": 0.9, "maintenance_cost_per_km": 0.1}]
        conn = connect(":memory:")
        self.addCleanup(conn.close)
        init_db(conn)
        with self.assertRaises(seed.DataError) as ctx:
            seed.new_game(conn, data_dir=self._data_dir(models=models))
        self.assertIn("hydrogene", str(ctx.exception))

    def test_default_data_is_valid(self):
        conn, cid = fresh_game(self)
        self.assertGreaterEqual(conn.execute("SELECT COUNT(*) FROM bus_model").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
