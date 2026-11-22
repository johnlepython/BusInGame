import unittest
from datetime import date

from busmanager import actions, engine, rules, seed, sim
from busmanager.db import connect, init_db

MONDAY = date(2026, 11, 2)


def fresh_game(test, seed_value=1):
    conn = connect(":memory:")
    test.addCleanup(conn.close)
    init_db(conn)
    cid = seed.new_game(conn, seed=seed_value, start_date=MONDAY)
    return conn, cid


def ledger_total(conn, cid):
    return conn.execute("SELECT COALESCE(SUM(amount), 0) AS t FROM ledger WHERE company_id = ?", (cid,)).fetchone()["t"]


def cash(conn, cid):
    return conn.execute("SELECT cash FROM company WHERE id = ?", (cid,)).fetchone()["cash"]


def model_id(conn, name):
    return conn.execute("SELECT id FROM bus_model WHERE name = ?", (name,)).fetchone()["id"]


def line_id(conn, code):
    return conn.execute("SELECT id FROM line WHERE code = ?", (code,)).fetchone()["id"]


def depot_id(conn):
    return conn.execute("SELECT id FROM depot").fetchone()["id"]


class NewGameTests(unittest.TestCase):
    def test_new_game_content(self):
        conn, cid = fresh_game(self)
        self.assertEqual(conn.execute("SELECT COUNT(*) AS n FROM vehicle").fetchone()["n"], 4)
        self.assertEqual(conn.execute("SELECT COUNT(*) AS n FROM line").fetchone()["n"], 4)
        self.assertEqual(conn.execute("SELECT COUNT(*) AS n FROM driver").fetchone()["n"], 8)
        self.assertEqual(cash(conn, cid), 300_000)
        self.assertEqual(cash(conn, cid), ledger_total(conn, cid))
        self.assertEqual(actions.option_stock(conn, cid, "valideur"), 2)
        tank = conn.execute("SELECT * FROM energy_infra WHERE energy = 'diesel'").fetchone()
        self.assertEqual((tank["capacity"], tank["stock"]), (12000, 6000))

    def test_start_fleet_is_used_and_two_have_validators(self):
        conn, _ = fresh_game(self)
        used = conn.execute("SELECT COUNT(*) FROM vehicle v JOIN bus_model m ON m.id = v.model_id "
                            "WHERE m.status = 'occasion'").fetchone()[0]
        self.assertEqual(used, 4)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM vehicle_option").fetchone()[0], 2)
        self.assertGreater(conn.execute("SELECT MIN(km) FROM vehicle").fetchone()[0], 100_000)


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
        self.assertLess(r["profit"], 0)

    def test_expired_ct_stops_the_bus(self):
        conn, cid = fresh_game(self)
        conn.execute("UPDATE vehicle SET ct_due_day = -1")
        self.assertEqual(engine.run_day(conn, cid)["served"], 0)

    def test_breakdown_sends_bus_to_workshop(self):
        conn, cid = fresh_game(self)
        conn.execute("UPDATE bus_model SET reliability = 0")
        engine.run_day(conn, cid)
        self.assertGreater(conn.execute("SELECT COUNT(*) FROM vehicle WHERE status = 'workshop'").fetchone()[0], 0)
        self.assertGreater(conn.execute("SELECT COUNT(*) FROM event_log WHERE kind='breakdown'").fetchone()[0], 0)

    def test_weekend_demand_is_lower(self):
        conn, cid = fresh_game(self)
        results = engine.catch_up(conn, cid, 7)
        self.assertLess(results[5]["demand"], results[0]["demand"])   # samedi
        self.assertLess(results[6]["demand"], results[5]["demand"])   # dimanche


class FrequencyTests(unittest.TestCase):
    def test_delivered_departures_capped_at_target_and_reported(self):
        conn, cid = fresh_game(self)
        r = engine.run_day(conn, cid)
        self.assertGreater(r["delivered_departures"], 0)
        self.assertLessEqual(r["delivered_departures"], r["target_departures"])
        rows = conn.execute("SELECT target_departures, delivered_departures FROM daily_report").fetchall()
        self.assertEqual(len(rows), 4)

    def test_adding_a_bus_raises_frequency_and_passengers(self):
        conn, cid = fresh_game(self)
        base = engine.run_day(conn, cid)
        conn.execute("UPDATE company SET cash = 5000000")
        vid = actions.buy_vehicle(conn, cid, model_id(conn, "Heuliez GX337 — diesel"), depot_id(conn))
        did = actions.hire_driver(conn, cid, "Renfort")
        sid = actions.create_service(conn, line_id(conn, "2"), 5, 14, "weekday")
        actions.assign_service(conn, sid, vehicle_id=vid, driver_id=did)
        more = engine.run_day(conn, cid)
        self.assertGreater(more["delivered_departures"], base["delivered_departures"])
        self.assertGreater(more["served"], base["served"])


class FraudAndOptionTests(unittest.TestCase):
    def test_validator_removes_fraud_loss(self):
        conn, cid = fresh_game(self)
        conn.execute("DELETE FROM vehicle_option")
        engine.run_day(conn, cid)
        fraud_without = conn.execute("SELECT SUM(fraud_loss) FROM daily_report").fetchone()[0]
        self.assertGreater(fraud_without, 0)

        conn2, cid2 = fresh_game(self)
        conn2.execute("UPDATE company SET cash = 5000000")
        actions.buy_option(conn2, cid2, "valideur", 5)
        for v in conn2.execute("SELECT id FROM vehicle WHERE id NOT IN (SELECT vehicle_id FROM vehicle_option)").fetchall():
            conn2.execute("UPDATE vehicle SET status='active'")
            actions.install_option(conn2, v["id"], "valideur")
        conn2.execute("UPDATE vehicle SET status = 'active'")  # on isole l'effet fraude de l'immobilisation
        engine.run_day(conn2, cid2)
        self.assertAlmostEqual(conn2.execute("SELECT SUM(fraud_loss) FROM daily_report").fetchone()[0], 0)

    def test_fraud_rate_is_ten_percent_of_gross(self):
        conn, cid = fresh_game(self)
        conn.execute("DELETE FROM vehicle_option")
        engine.run_day(conn, cid)
        rev, fraud = conn.execute("SELECT SUM(revenue), SUM(fraud_loss) FROM daily_report").fetchone()
        self.assertAlmostEqual(fraud / (rev + fraud), rules.FRAUD_RATE, places=6)

    def test_install_immobilises_bus_one_day_and_consumes_stock(self):
        conn, cid = fresh_game(self)
        vid = conn.execute("SELECT vehicle_id AS id FROM service WHERE vehicle_id NOT IN "
                           "(SELECT vehicle_id FROM vehicle_option) LIMIT 1").fetchone()["id"]
        stock = actions.option_stock(conn, cid, "valideur")
        actions.install_option(conn, vid, "valideur")
        self.assertEqual(actions.option_stock(conn, cid, "valideur"), stock - 1)
        v = conn.execute("SELECT * FROM vehicle WHERE id = ?", (vid,)).fetchone()
        self.assertEqual((v["status"], v["available_from_day"]), ("workshop", 1))
        d0, d1 = engine.catch_up(conn, cid, 2)
        # jour 0 : le bus est à l'atelier, jour 1 il revient
        self.assertEqual(conn.execute("SELECT status FROM vehicle WHERE id = ?", (vid,)).fetchone()[0], "active")
        self.assertGreater(d1["served"], d0["served"] * 0.5)

    def test_install_needs_stock_and_no_duplicate(self):
        conn, cid = fresh_game(self)
        free = [r["id"] for r in conn.execute(
            "SELECT id FROM vehicle WHERE id NOT IN (SELECT vehicle_id FROM vehicle_option)")]
        actions.install_option(conn, free[0], "valideur")
        conn.execute("UPDATE vehicle SET status = 'active'")
        with self.assertRaises(actions.GameError):   # déjà équipé
            actions.install_option(conn, free[0], "valideur")
        actions.install_option(conn, free[1], "valideur")
        conn.execute("UPDATE vehicle SET status = 'active'")
        self.assertEqual(actions.option_stock(conn, cid, "valideur"), 0)
        conn.execute("DELETE FROM vehicle_option WHERE vehicle_id = ?", (free[1],))
        with self.assertRaises(actions.GameError):   # stock vide
            actions.install_option(conn, free[1], "valideur")

    def test_buy_option_costs_money_and_fills_stock(self):
        conn, cid = fresh_game(self)
        before = cash(conn, cid)
        actions.buy_option(conn, cid, "valideur", 3)
        self.assertEqual(actions.option_stock(conn, cid, "valideur"), 5)
        self.assertAlmostEqual(before - cash(conn, cid), 3 * 1400)
        self.assertAlmostEqual(cash(conn, cid), ledger_total(conn, cid), places=4)
        with self.assertRaises(actions.GameError):
            actions.buy_option(conn, cid, "valideur", 0)


class FuelTests(unittest.TestCase):
    def test_tank_is_drawn_down_and_cheaper_than_station(self):
        conn, cid = fresh_game(self)
        before = conn.execute("SELECT stock FROM energy_infra WHERE energy='diesel'").fetchone()[0]
        engine.run_day(conn, cid)
        used = conn.execute("SELECT quantity FROM energy_use WHERE energy = 'diesel'").fetchone()[0]
        after = conn.execute("SELECT stock FROM energy_infra WHERE energy='diesel'").fetchone()[0]
        self.assertAlmostEqual(before - after, used)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM event_log WHERE kind='station'").fetchone()[0], 0)

    def test_empty_tank_buys_at_station_with_premium(self):
        conn, cid = fresh_game(self)
        conn.execute("UPDATE energy_infra SET stock = 0")
        engine.run_day(conn, cid)
        used = conn.execute("SELECT quantity FROM energy_use WHERE energy = 'diesel'").fetchone()[0]
        paid = -conn.execute("SELECT SUM(amount) FROM ledger WHERE kind = 'fuel'").fetchone()[0]
        self.assertAlmostEqual(paid, used * rules.ENERGY_PRICE["diesel"] * rules.EXTERNAL_FUEL_PREMIUM["diesel"], places=2)
        self.assertGreater(conn.execute("SELECT COUNT(*) FROM event_log WHERE kind='station'").fetchone()[0], 0)

    def test_no_tank_at_all_also_pays_premium(self):
        conn, cid = fresh_game(self)
        conn.execute("DELETE FROM energy_infra")
        engine.run_day(conn, cid)
        used = conn.execute("SELECT quantity FROM energy_use WHERE energy = 'diesel'").fetchone()[0]
        paid = -conn.execute("SELECT SUM(amount) FROM ledger WHERE kind = 'fuel'").fetchone()[0]
        self.assertAlmostEqual(paid, used * 1.6 * 1.3, places=2)

    def test_build_tank_and_fill(self):
        conn, cid = fresh_game(self)
        actions.post(conn, cid, 0, "capital", 700_000, "Apport")
        before = cash(conn, cid)
        actions.build_tank(conn, cid, depot_id(conn), "gnv", 5000)
        self.assertAlmostEqual(before - cash(conn, cid), 5000 * rules.TANK_COST_PER_UNIT["gnv"])
        actions.buy_fuel(conn, cid, depot_id(conn), "gnv", 2000)
        self.assertEqual(conn.execute("SELECT stock FROM energy_infra WHERE energy='gnv'").fetchone()[0], 2000)
        with self.assertRaises(actions.GameError):   # citerne trop pleine
            actions.buy_fuel(conn, cid, depot_id(conn), "gnv", 4000)
        with self.assertRaises(actions.GameError):   # pas de citerne d'électricité
            actions.build_tank(conn, cid, depot_id(conn), "electrique", 100)
        self.assertAlmostEqual(cash(conn, cid), ledger_total(conn, cid), places=4)

    def test_low_tank_alert(self):
        conn, cid = fresh_game(self)
        conn.execute("UPDATE energy_infra SET stock = 500")
        engine.run_day(conn, cid)
        self.assertGreater(conn.execute(
            "SELECT COUNT(*) FROM event_log WHERE kind IN ('low_fuel', 'station')").fetchone()[0], 0)


class ElectricTests(unittest.TestCase):
    def _electric_setup(self, conn, cid, range_name="Heuliez GX337 ELEC — 200 km"):
        conn.execute("UPDATE company SET cash = 5000000")
        vid = actions.buy_vehicle(conn, cid, model_id(conn, range_name), depot_id(conn))
        did = actions.hire_driver(conn, cid, "Élec")
        sid = actions.create_service(conn, line_id(conn, "2"), 5, 14, "all")
        actions.assign_service(conn, sid, vehicle_id=vid, driver_id=did)
        return vid

    def test_electric_bus_needs_a_charger(self):
        conn, cid = fresh_game(self)
        vid = self._electric_setup(conn, cid)
        engine.run_day(conn, cid)
        self.assertEqual(conn.execute("SELECT km FROM vehicle WHERE id = ?", (vid,)).fetchone()[0], 0)
        self.assertGreater(conn.execute("SELECT COUNT(*) FROM event_log WHERE kind='no_charger'").fetchone()[0], 0)

    def test_electric_bus_runs_with_a_charger_and_pays_electricity(self):
        conn, cid = fresh_game(self)
        vid = self._electric_setup(conn, cid)
        actions.build_chargers(conn, cid, depot_id(conn), 1)
        engine.run_day(conn, cid)
        km = conn.execute("SELECT km FROM vehicle WHERE id = ?", (vid,)).fetchone()[0]
        self.assertGreater(km, 0)
        self.assertGreater(conn.execute("SELECT quantity FROM energy_use WHERE energy='electrique'").fetchone()[0], 0)

    def test_daily_km_never_exceeds_range(self):
        conn, cid = fresh_game(self)
        vid = self._electric_setup(conn, cid)
        actions.build_chargers(conn, cid, depot_id(conn), 1)
        engine.run_day(conn, cid)
        km = conn.execute("SELECT km FROM vehicle WHERE id = ?", (vid,)).fetchone()[0]
        self.assertLessEqual(km, 200)

    def test_battery_empty_event_when_range_too_short(self):
        conn, cid = fresh_game(self)
        vid = self._electric_setup(conn, cid)
        actions.build_chargers(conn, cid, depot_id(conn), 1)
        conn.execute("UPDATE bus_model SET range_km = 40 WHERE energy = 'electrique'")
        engine.run_day(conn, cid)
        km = conn.execute("SELECT km FROM vehicle WHERE id = ?", (vid,)).fetchone()[0]
        self.assertLessEqual(km, 40)
        self.assertGreater(conn.execute("SELECT COUNT(*) FROM event_log WHERE kind='battery'").fetchone()[0], 0)

    def test_chargers_cost_and_capacity(self):
        conn, cid = fresh_game(self)
        conn.execute("UPDATE company SET cash = 500000")
        before = cash(conn, cid)
        actions.build_chargers(conn, cid, depot_id(conn), 2)
        self.assertAlmostEqual(before - cash(conn, cid), 2 * rules.CHARGER_COST)
        self.assertEqual(conn.execute("SELECT capacity FROM energy_infra WHERE energy='electrique'").fetchone()[0], 2)


class ActionTests(unittest.TestCase):
    def test_buy_needs_cash_and_depot_room(self):
        conn, cid = fresh_game(self)
        art = model_id(conn, "Heuliez GX437 ELEC — 200 km")
        conn.execute("UPDATE company SET cash = 1000")
        with self.assertRaises(actions.GameError):
            actions.buy_vehicle(conn, cid, art, depot_id(conn))
        conn.execute("UPDATE company SET cash = 5000000")
        conn.execute("UPDATE depot SET capacity = 4")
        with self.assertRaises(actions.GameError):
            actions.buy_vehicle(conn, cid, art, depot_id(conn))

    def test_used_bus_starts_worn(self):
        conn, cid = fresh_game(self)
        conn.execute("UPDATE company SET cash = 5000000")
        vid = actions.buy_vehicle(conn, cid, model_id(conn, "Irisbus Citelis 12 — diesel"), depot_id(conn))
        v = conn.execute("SELECT * FROM vehicle WHERE id = ?", (vid,)).fetchone()
        self.assertEqual((v["km"], v["engine_state"], v["interior_state"]), (420000, 55, 45))
        self.assertEqual((v["ct_due_day"], v["revision_due_day"]), (150, 40))

    def test_new_bus_starts_pristine(self):
        conn, cid = fresh_game(self)
        conn.execute("UPDATE company SET cash = 5000000")
        vid = actions.buy_vehicle(conn, cid, model_id(conn, "Heuliez GX337 — diesel"), depot_id(conn))
        v = conn.execute("SELECT * FROM vehicle WHERE id = ?", (vid,)).fetchone()
        self.assertEqual((v["km"], v["engine_state"], v["interior_state"]), (0, 100, 100))

    def test_articulated_bus_forbidden_on_standard_only_line(self):
        conn, cid = fresh_game(self)
        conn.execute("UPDATE company SET cash = 5000000")
        vid = actions.buy_vehicle(conn, cid, model_id(conn, "Heuliez GX437 — diesel"), depot_id(conn))
        sid = actions.create_service(conn, line_id(conn, "2"), 6, 10)
        with self.assertRaises(actions.GameError):
            actions.assign_service(conn, sid, vehicle_id=vid)

    def test_midi_allowed_on_line_3_but_not_line_4(self):
        conn, cid = fresh_game(self)
        conn.execute("UPDATE company SET cash = 5000000")
        vid = actions.buy_vehicle(conn, cid, model_id(conn, "Heuliez GX137 — diesel"), depot_id(conn))
        ok = actions.create_service(conn, line_id(conn, "3"), 6, 10, "saturday")
        actions.assign_service(conn, ok, vehicle_id=vid)
        bad = actions.create_service(conn, line_id(conn, "4"), 6, 10, "saturday")
        with self.assertRaises(actions.GameError):
            actions.assign_service(conn, bad, vehicle_id=vid)

    def test_articulated_needs_licence(self):
        conn, cid = fresh_game(self)
        conn.execute("UPDATE company SET cash = 5000000")
        vid = actions.buy_vehicle(conn, cid, model_id(conn, "Heuliez GX437 — diesel"), depot_id(conn))
        sid = actions.create_service(conn, line_id(conn, "1"), 6, 10, "saturday")
        no_licence = actions.hire_driver(conn, cid, "Sans permis")
        actions.assign_service(conn, sid, vehicle_id=vid)
        with self.assertRaises(actions.GameError):
            actions.assign_service(conn, sid, driver_id=no_licence)
        with_licence = actions.hire_driver(conn, cid, "Avec permis", articulated_licence=True)
        actions.assign_service(conn, sid, driver_id=with_licence)

    def test_driver_hour_limit_and_overlap(self):
        conn, cid = fresh_game(self)
        l3 = line_id(conn, "3")
        d = actions.hire_driver(conn, cid, "Test")
        s1 = actions.create_service(conn, l3, 6, 14)
        s2 = actions.create_service(conn, l3, 14, 16)
        s3 = actions.create_service(conn, l3, 10, 12)
        actions.assign_service(conn, s1, driver_id=d)
        with self.assertRaises(actions.GameError):
            actions.assign_service(conn, s2, driver_id=d)
        with self.assertRaises(actions.GameError):
            actions.assign_service(conn, s3, driver_id=d)

    def test_service_longer_than_limit_rejected(self):
        conn, cid = fresh_game(self)
        with self.assertRaises(actions.GameError):
            actions.create_service(conn, line_id(conn, "3"), 5, 20)

    def test_revision_and_ct_update_dates_and_cash(self):
        conn, cid = fresh_game(self)
        vid = conn.execute("SELECT id FROM vehicle ORDER BY id LIMIT 1").fetchone()["id"]
        conn.execute("UPDATE vehicle SET engine_state = 60 WHERE id = ?", (vid,))
        before = cash(conn, cid)
        actions.maintain_vehicle(conn, vid)
        actions.pass_ct(conn, vid)
        v = conn.execute("SELECT * FROM vehicle WHERE id = ?", (vid,)).fetchone()
        self.assertEqual(v["engine_state"], 60 + rules.REVISION_ENGINE_BONUS)
        self.assertEqual(v["status"], "workshop")
        self.assertAlmostEqual(before - cash(conn, cid), rules.REVISION_COST + rules.CT_COST)
        self.assertAlmostEqual(cash(conn, cid), ledger_total(conn, cid), places=4)


class ReputationTests(unittest.TestCase):
    def test_reputation_moves_toward_target(self):
        conn, cid = fresh_game(self)
        engine.catch_up(conn, cid, 30)
        rep = conn.execute("SELECT reputation FROM company").fetchone()[0]
        self.assertNotEqual(rep, rules.REPUTATION_START)
        self.assertTrue(0 <= rep <= 100)


class SimulationTests(unittest.TestCase):
    def test_greedy_strategy_is_profitable_and_grows_fleet(self):
        r = sim.simulate(days=240, seed_value=1, verbose=False)
        self.addCleanup(r["conn"].close)
        operating = r["conn"].execute(
            "SELECT SUM(amount) FROM ledger WHERE kind NOT IN ('purchase', 'capital')").fetchone()[0]
        self.assertGreater(operating, 0)   # l'exploitation rapporte ; la trésorerie est réinvestie en bus
        self.assertGreater(r["conn"].execute("SELECT COUNT(*) FROM vehicle").fetchone()[0], 4)

    def test_doing_nothing_loses_money_slowly(self):
        r = sim.simulate(days=30, seed_value=1, strategy=None, verbose=False)
        self.addCleanup(r["conn"].close)
        self.assertGreater(r["cash"], 0)


if __name__ == "__main__":
    unittest.main()
