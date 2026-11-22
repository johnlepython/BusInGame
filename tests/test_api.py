import os
import tempfile
import unittest

try:
    from fastapi.testclient import TestClient
    from busmanager.api import create_app
    HAVE_API = True
except ImportError:  # dépendances web non installées : on saute ces tests
    HAVE_API = False

PASSWORD = "motdepasse1"


@unittest.skipUnless(HAVE_API, "fastapi/httpx non installés (pip install -r requirements.txt)")
class ApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app = create_app(os.path.join(self.tmp.name, "t.db"), dev=True)
        self.client = TestClient(self.app)
        r = self.client.post("/api/register", json={"username": "bob", "password": PASSWORD})
        self.assertEqual(r.status_code, 201, r.text)

    def other_client(self, name="alice"):
        c = TestClient(self.app)
        self.assertEqual(c.post("/api/register", json={"username": name, "password": PASSWORD}).status_code, 201)
        return c

    def get(self, path, client=None):
        r = (client or self.client).get("/api" + path)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    # ----- comptes -----

    def test_routes_require_login(self):
        anon = TestClient(self.app)
        for path in ("/state", "/vehicles", "/drivers", "/lines", "/infra", "/report", "/events", "/models", "/me"):
            self.assertEqual(anon.get("/api" + path).status_code, 401, path)
        self.assertEqual(anon.post("/api/tick", json={"days": 1}).status_code, 401)
        self.assertEqual(anon.post("/api/vehicles", json={"model_id": 1}).status_code, 401)

    def test_register_login_logout(self):
        self.assertEqual(self.get("/me")["username"], "bob")
        self.client.post("/api/logout")
        self.assertEqual(self.client.get("/api/me").status_code, 401)
        bad = self.client.post("/api/login", json={"username": "bob", "password": "faux"})
        self.assertEqual(bad.status_code, 401)
        unknown = self.client.post("/api/login", json={"username": "nobody", "password": PASSWORD})
        self.assertEqual(unknown.status_code, 401)
        self.assertEqual(bad.json()["detail"], unknown.json()["detail"])  # ne révèle pas si le compte existe
        ok = self.client.post("/api/login", json={"username": "BOB", "password": PASSWORD})
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(self.get("/me")["username"], "bob")

    def test_register_validation_and_duplicates(self):
        c = TestClient(self.app)
        self.assertEqual(c.post("/api/register", json={"username": "ab", "password": PASSWORD}).status_code, 400)
        self.assertEqual(c.post("/api/register", json={"username": "carol", "password": "court"}).status_code, 400)
        self.assertEqual(c.post("/api/register", json={"username": "a b c", "password": PASSWORD}).status_code, 400)
        dup = c.post("/api/register", json={"username": "BOB", "password": PASSWORD})
        self.assertEqual(dup.status_code, 400)
        self.assertIn("déjà pris", dup.json()["detail"])

    def test_session_cookie_is_httponly_and_token_not_stored(self):
        r = TestClient(self.app).post("/api/register", json={"username": "dave", "password": PASSWORD})
        cookie = r.headers["set-cookie"].lower()
        self.assertIn("httponly", cookie)
        self.assertIn("samesite=lax", cookie)
        token = r.cookies.get("bm_session")
        from busmanager.db import connect
        conn = connect(os.path.join(self.tmp.name, "t.db"))
        self.addCleanup(conn.close)
        hashes = [row[0] for row in conn.execute("SELECT token_hash FROM session")]
        self.assertNotIn(token, hashes)
        self.assertTrue(all(len(h) == 64 for h in hashes))
        stored = conn.execute("SELECT password_hash FROM account WHERE username = 'dave'").fetchone()[0]
        self.assertTrue(stored.startswith("scrypt$"))
        self.assertNotIn(PASSWORD, stored)

    def test_expired_session_is_refused(self):
        from busmanager.db import connect
        conn = connect(os.path.join(self.tmp.name, "t.db"))
        self.addCleanup(conn.close)
        conn.execute("UPDATE session SET expires_at = '2000-01-01T00:00:00+00:00'")
        conn.commit()
        self.assertEqual(self.client.get("/api/me").status_code, 401)

    def test_each_player_gets_their_own_company(self):
        alice = self.other_client()
        self.assertEqual(len(self.get("/vehicles")), 4)
        self.assertEqual(len(self.get("/vehicles", alice)), 4)
        bob_ids = {v["id"] for v in self.get("/vehicles")}
        alice_ids = {v["id"] for v in self.get("/vehicles", alice)}
        self.assertFalse(bob_ids & alice_ids)
        self.client.post("/api/tick", json={"days": 2})
        self.assertEqual(self.get("/state")["game_day"], 2)
        self.assertEqual(self.get("/state", alice)["game_day"], 0)

    def test_cannot_touch_another_players_objects(self):
        alice = self.other_client()
        bob_vehicle = self.get("/vehicles")[0]["id"]
        bob_service = self.get("/lines")[0]["services"][0]["id"]
        bob_line = self.get("/lines")[0]["id"]
        bob_driver = self.get("/drivers")[0]["id"]
        bob_depot = self.get("/infra")["depots"][0]["id"]
        self.assertEqual(alice.post(f"/api/vehicles/{bob_vehicle}/revision").status_code, 404)
        self.assertEqual(alice.delete(f"/api/services/{bob_service}").status_code, 404)
        self.assertEqual(alice.patch(f"/api/services/{bob_service}", json={"unassign_driver": True}).status_code, 404)
        self.assertEqual(alice.post("/api/services", json={"line_id": bob_line, "start_hour": 6, "end_hour": 8}).status_code, 404)
        self.assertEqual(alice.post("/api/options/install", json={"vehicle_id": bob_vehicle, "code": "valideur"}).status_code, 404)
        self.assertEqual(alice.post("/api/infra/fuel", json={"depot_id": bob_depot, "energy": "diesel", "quantity": 10}).status_code, 404)
        self.assertEqual(alice.post("/api/infra/chargers", json={"depot_id": bob_depot, "count": 1}).status_code, 404)
        alice_service = self.get("/lines", alice)[0]["services"][0]["id"]
        r = alice.patch(f"/api/services/{alice_service}", json={"vehicle_id": bob_vehicle})
        self.assertEqual(r.status_code, 404)
        r = alice.patch(f"/api/services/{alice_service}", json={"driver_id": bob_driver})
        self.assertEqual(r.status_code, 404)
        # rien n'a bougé chez Bob
        self.assertEqual(self.get("/vehicles")[0]["status"], "active")

    # ----- jeu -----

    def test_state_and_lists(self):
        st = self.get("/state")
        self.assertEqual(st["game_day"], 0)
        self.assertIn(st["day_type"], ("weekday", "saturday", "sunday"))
        self.assertEqual(len(self.get("/drivers")), 8)
        lines = self.get("/lines")
        self.assertEqual(len(lines), 4)
        l1 = lines[0]
        self.assertEqual(l1["frequency"]["weekday"]["pointe"], 4)
        self.assertEqual(l1["buses_needed"]["weekday"]["pointe"], 6)  # 45 min : 4 départs/h = 6 bus
        self.assertTrue(l1["services"])
        self.assertEqual(len(self.get("/models")), 44)

    def test_infra_and_options(self):
        infra = self.get("/infra")
        depot = infra["depots"][0]
        diesel = [i for i in depot["infra"] if i["energy"] == "diesel"][0]
        self.assertEqual((diesel["capacity"], diesel["stock"]), (12000, 6000))
        opt = infra["options"][0]
        self.assertEqual((opt["code"], opt["stock"], opt["installed"]), ("valideur", 2, 2))

    def test_option_purchase_and_install_flow(self):
        self.assertEqual(self.client.post("/api/options/buy", json={"code": "valideur", "quantity": 3}).status_code, 200)
        self.assertEqual(self.get("/infra")["options"][0]["stock"], 5)
        bare = [v for v in self.get("/vehicles") if "valideur" not in v["options"]][0]
        r = self.client.post("/api/options/install", json={"vehicle_id": bare["id"], "code": "valideur"})
        self.assertEqual(r.status_code, 200, r.text)
        after = [v for v in self.get("/vehicles") if v["id"] == bare["id"]][0]
        self.assertEqual(after["status"], "workshop")
        self.assertIn("valideur", after["options"])
        again = self.client.post("/api/options/install", json={"vehicle_id": bare["id"], "code": "valideur"})
        self.assertEqual(again.status_code, 400)
        self.assertEqual(self.client.post("/api/options/buy", json={"code": "inconnue", "quantity": 1}).status_code, 404)

    def test_tank_chargers_and_fuel(self):
        depot = self.get("/infra")["depots"][0]["id"]
        self.assertEqual(self.client.post("/api/infra/tank", json={"depot_id": depot, "energy": "gnv", "capacity": 4000}).status_code, 200)
        self.assertEqual(self.client.post("/api/infra/fuel", json={"depot_id": depot, "energy": "gnv", "quantity": 1000}).status_code, 200)
        self.assertEqual(self.client.post("/api/infra/chargers", json={"depot_id": depot, "count": 1}).status_code, 200)
        energies = {i["energy"]: i for i in self.get("/infra")["depots"][0]["infra"]}
        self.assertEqual(energies["gnv"]["stock"], 1000)
        self.assertEqual(energies["electrique"]["capacity"], 1)
        too_much = self.client.post("/api/infra/fuel", json={"depot_id": depot, "energy": "gnv", "quantity": 99999})
        self.assertEqual(too_much.status_code, 400)
        self.assertEqual(self.client.post("/api/infra/tank", json={"depot_id": depot, "energy": "electrique", "capacity": 10}).status_code, 400)

    def test_parallel_requests(self):
        from concurrent.futures import ThreadPoolExecutor
        paths = ["/state", "/models", "/vehicles", "/drivers", "/lines", "/infra", "/report", "/events"] * 6
        with ThreadPoolExecutor(max_workers=8) as pool:
            codes = list(pool.map(lambda p: self.client.get("/api" + p).status_code, paths))
        self.assertEqual(set(codes), {200})

    def test_index_page_and_static(self):
        anon = TestClient(self.app)
        self.assertEqual(anon.get("/").status_code, 200)
        self.assertEqual(anon.get("/static/app.js").status_code, 200)
        self.assertEqual(anon.get("/static/style.css").status_code, 200)

    def test_tick_then_report(self):
        self.assertEqual(self.get("/report")["day"], None)
        r = self.client.post("/api/tick", json={"days": 3})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.json()["days"]), 3)
        rep = self.get("/report")
        self.assertEqual(rep["day"], 2)
        self.assertEqual(len(rep["history"]), 3)
        self.assertIn("fraud_loss", rep["lines"][0])
        self.assertIn("target_departures", rep["lines"][0])
        self.assertEqual(self.get("/state")["game_day"], 3)

    def test_tick_disabled_outside_dev(self):
        client = TestClient(create_app(os.path.join(self.tmp.name, "prod.db"), dev=False))
        client.post("/api/register", json={"username": "erin", "password": PASSWORD})
        self.assertEqual(client.post("/api/tick", json={"days": 1}).status_code, 403)

    def test_secure_cookie_flag(self):
        app = create_app(os.path.join(self.tmp.name, "s.db"), dev=False, secure_cookies=True)
        r = TestClient(app).post("/api/register", json={"username": "frank", "password": PASSWORD})
        self.assertIn("secure", r.headers["set-cookie"].lower())

    def test_buy_vehicle_and_errors_are_400(self):
        models = self.get("/models")
        r = self.client.post("/api/vehicles", json={"model_id": models[0]["id"]})
        self.assertEqual(r.status_code, 201, r.text)
        self.assertEqual(len(self.get("/vehicles")), 5)
        r = self.client.post("/api/vehicles", json={"model_id": models[-1]["id"]})
        self.assertEqual(r.status_code, 400)
        self.assertIn("Trésorerie", r.json()["detail"])

    def test_hire_and_service_workflow(self):
        d = self.client.post("/api/drivers", json={"name": "Alice"}).json()["id"]
        line = self.get("/lines")[1]
        s = self.client.post("/api/services", json={"line_id": line["id"], "start_hour": 6, "end_hour": 10, "day_type": "saturday"})
        self.assertEqual(s.status_code, 201)
        sid = s.json()["id"]
        self.assertEqual(self.client.patch(f"/api/services/{sid}", json={"driver_id": d}).status_code, 200)
        l2 = [x for x in self.get("/lines") if x["id"] == line["id"]][0]
        self.assertIn("Alice", [x["driver_name"] for x in l2["services"]])
        self.assertEqual(self.client.patch(f"/api/services/{sid}", json={"unassign_driver": True}).status_code, 200)
        self.assertEqual(self.client.delete(f"/api/services/{sid}").status_code, 200)
        self.assertEqual(self.client.delete(f"/api/services/{sid}").status_code, 404)

    def test_validation_errors(self):
        self.assertEqual(self.client.post("/api/drivers", json={"name": ""}).status_code, 422)
        self.assertEqual(self.client.post("/api/tick", json={"days": 0}).status_code, 422)
        self.assertEqual(self.client.post("/api/vehicles/1/inconnue").status_code, 404)

    def test_vehicle_actions(self):
        vid = self.get("/vehicles")[0]["id"]
        self.assertEqual(self.client.post(f"/api/vehicles/{vid}/ct").status_code, 200)
        self.assertEqual(self.client.post(f"/api/vehicles/{vid}/revision").status_code, 200)
        self.assertEqual(self.client.post(f"/api/vehicles/{vid}/overhaul").status_code, 400)
        self.assertEqual(self.get("/vehicles")[0]["status"], "workshop")


if __name__ == "__main__":
    unittest.main()
