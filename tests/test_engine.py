import unittest

from datetime import date

from busmanager import actions, engine, rules, seed, sim
from busmanager.db import connect, init_db


def fresh_game(test, seed_value=1):
    conn = connect(":memory:")
    test.addCleanup(conn.close)
    init_db(conn)
    cid = seed.new_game(conn, seed=seed_value, start_date=date(2026, 11, 2))  # un lundi
    return conn, cid


def ledger_total(conn, cid):
    return conn.execute("SELECT COALESCE(SUM(amount), 0) AS t FROM ledger WHERE company_id = ?", (cid,)).fetchone()["t"]


def cash(conn, cid):
    return conn.execute("SELECT cash FROM company WHERE id = ?", (cid,)).fetchone()["cash"]


class NewGameTests(unittest.TestCase):
    def test_new_game_content(self):
        conn, cid = fresh_game(self)
        self.assertEqual(conn.execute("SELECT COUNT(*) AS n FROM vehicle").fetchone()["n"], 1)
        self.assertEqual(conn.execute("SELECT COUNT(*) AS n FROM line").fetchone()["n"], 3)
        self.assertEqual(conn.execute("SELECT COUNT(*) AS n FROM driver").fetchone()["n"], 2)
        self.assertEqual(cash(conn, cid), ledger_total(conn, cid))


class TickTests(unittest.TestCase):
    def test_tick_advances_day_and_keeps_ledger_invariant(self):
        conn, cid = fresh_game(self)
        engine.catch_up(conn, cid, 10)
        self.assertEqual(actions.current_day(conn, cid), 10)
        self.assertAlmostEqual(cash(conn, cid), ledger_total(conn, cid), places=4)

    def test_catch_up_is_idempotent(self):
        conn, cid = fresh_game(self)
        engine.catch_up(conn, cid, 5)
        before = cash(conn, cid)
        self.assertEqual(engine.catch_up(conn, cid, 5), [])
        self.assertEqual(cash(conn, cid), before)

    def test_run_day_refuses_a_day_already_logged(self):
        conn, cid = fresh_game(self)
        engine.run_day(conn, cid)
        conn.execute("UPDATE company SET game_day = 0 WHERE id = ?", (cid,))
        with self.assertRaises(engine.AlreadyTicked):
            engine.run_day(conn, cid)

    def test_same_seed_same_result(self):
        runs = [sim.simulate(days=60, seed_value=7, verbose=False) for _ in range(2)]
        for r in runs:
            self.addCleanup(r["conn"].close)
        self.assertEqual(runs[0]["cash"], runs[1]["cash"])

    def test_service_without_driver_earns_nothing(self):
        conn, cid = fresh_game(self)
        conn.execute("UPDATE service SET driver_id = NULL")
        r = engine.run_day(conn, cid)
        self.assertEqual(r["served"], 0)
        self.assertLess(r["profit"], 0)  # il reste les salaires et le dépôt

    def test_expired_ct_stops_the_bus(self):
        conn, cid = fresh_game(self)
        conn.execute("UPDATE vehicle SET ct_due_day = -1")
        r = engine.run_day(conn, cid)
        self.assertEqual(r["served"], 0)

    def test_breakdown_sends_bus_to_workshop(self):
        conn, cid = fresh_game(self)
        conn.execute("UPDATE bus_model SET reliability = 0")  # panne quasi certaine (p plafonnée à 0.9)
        for _ in range(10):
            engine.run_day(conn, cid)
            if conn.execute("SELECT status FROM vehicle").fetchone()["status"] == "workshop":
                break
        self.assertEqual(conn.execute("SELECT status FROM vehicle").fetchone()["status"], "workshop")
        self.assertGreater(conn.execute("SELECT COUNT(*) AS n FROM event_log WHERE kind='breakdown'").fetchone()["n"], 0)

    def test_weekend_demand_is_lower(self):
        conn, cid = fresh_game(self)
        results = engine.catch_up(conn, cid, 7)
        self.assertLess(results[5]["demand"], results[0]["demand"])


class ActionTests(unittest.TestCase):
    def test_buy_needs_cash_and_depot_room(self):
        conn, cid = fresh_game(self)
        depot_id = conn.execute("SELECT id FROM depot").fetchone()["id"]
        art = conn.execute("SELECT id FROM bus_model WHERE name = 'Articulé 18 m électrique'").fetchone()["id"]
        conn.execute("UPDATE company SET cash = 1000")
        with self.assertRaises(actions.GameError):
            actions.buy_vehicle(conn, cid, art, depot_id)
        conn.execute("UPDATE company SET cash = 5000000")
        conn.execute("UPDATE depot SET capacity = 1")
        with self.assertRaises(actions.GameError):
            actions.buy_vehicle(conn, cid, art, depot_id)

    def test_articulated_bus_forbidden_on_standard_only_line(self):
        conn, cid = fresh_game(self)
        depot_id = conn.execute("SELECT id FROM depot").fetchone()["id"]
        art = conn.execute("SELECT id FROM bus_model WHERE body = 'articule' AND energy = 'diesel'").fetchone()["id"]
        conn.execute("UPDATE company SET cash = 5000000")
        vid = actions.buy_vehicle(conn, cid, art, depot_id)
        l2 = conn.execute("SELECT id FROM line WHERE code = 'L2'").fetchone()["id"]
        sid = actions.create_service(conn, l2, 6, 10)
        with self.assertRaises(actions.GameError):
            actions.assign_service(conn, sid, vehicle_id=vid)

    def test_articulated_needs_licence(self):
        conn, cid = fresh_game(self)
        depot_id = conn.execute("SELECT id FROM depot").fetchone()["id"]
        art = conn.execute("SELECT id FROM bus_model WHERE body = 'articule' AND energy = 'diesel'").fetchone()["id"]
        conn.execute("UPDATE company SET cash = 5000000")
        vid = actions.buy_vehicle(conn, cid, art, depot_id)
        l1 = conn.execute("SELECT id FROM line WHERE code = 'L1'").fetchone()["id"]
        sid = actions.create_service(conn, l1, 6, 10)
        no_licence = actions.hire_driver(conn, cid, "Sans permis")
        actions.assign_service(conn, sid, vehicle_id=vid)
        with self.assertRaises(actions.GameError):
            actions.assign_service(conn, sid, driver_id=no_licence)
        with_licence = actions.hire_driver(conn, cid, "Avec permis", articulated_licence=True)
        actions.assign_service(conn, sid, driver_id=with_licence)

    def test_driver_hour_limit_and_overlap(self):
        conn, cid = fresh_game(self)
        l3 = conn.execute("SELECT id FROM line WHERE code = 'L3'").fetchone()["id"]
        d = actions.hire_driver(conn, cid, "Test")
        s1 = actions.create_service(conn, l3, 6, 14)       # 8 h
        s2 = actions.create_service(conn, l3, 14, 16)      # +2 h = 10 h > 9
        s3 = actions.create_service(conn, l3, 10, 12)      # chevauche s1
        actions.assign_service(conn, s1, driver_id=d)
        with self.assertRaises(actions.GameError):
            actions.assign_service(conn, s2, driver_id=d)
        with self.assertRaises(actions.GameError):
            actions.assign_service(conn, s3, driver_id=d)

    def test_service_longer_than_limit_rejected(self):
        conn, cid = fresh_game(self)
        l3 = conn.execute("SELECT id FROM line WHERE code = 'L3'").fetchone()["id"]
        with self.assertRaises(actions.GameError):
            actions.create_service(conn, l3, 5, 20)

    def test_revision_and_ct_update_dates_and_cash(self):
        conn, cid = fresh_game(self)
        vid = conn.execute("SELECT id FROM vehicle").fetchone()["id"]
        conn.execute("UPDATE vehicle SET engine_state = 60")
        before = cash(conn, cid)
        actions.maintain_vehicle(conn, vid)
        actions.pass_ct(conn, vid)
        v = conn.execute("SELECT * FROM vehicle WHERE id = ?", (vid,)).fetchone()
        self.assertEqual(v["engine_state"], 60 + rules.REVISION_ENGINE_BONUS)
        self.assertEqual(v["status"], "workshop")
        self.assertAlmostEqual(before - cash(conn, cid), rules.REVISION_COST + rules.CT_COST)
        self.assertAlmostEqual(cash(conn, cid), ledger_total(conn, cid), places=4)


if __name__ == "__main__":
    unittest.main()
