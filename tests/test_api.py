import os
import tempfile
import unittest

try:
    from fastapi.testclient import TestClient
    from busmanager.api import create_app
    HAVE_API = True
except ImportError:  # dépendances web non installées : on saute ces tests
    HAVE_API = False


@unittest.skipUnless(HAVE_API, "fastapi/httpx non installés (pip install -r requirements.txt)")
class ApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.client = TestClient(create_app(os.path.join(self.tmp.name, "t.db"), dev=True))

    def get(self, path):
        r = self.client.get("/api" + path)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def test_state_and_lists(self):
        st = self.get("/state")
        self.assertEqual(st["game_day"], 0)
        self.assertEqual(len(self.get("/vehicles")), 1)
        self.assertEqual(len(self.get("/drivers")), 2)
        lines = self.get("/lines")
        self.assertEqual(len(lines), 3)
        self.assertTrue(lines[0]["services"])
        self.assertTrue(self.get("/models"))

    def test_parallel_requests(self):
        """Le navigateur charge plusieurs endpoints en parallèle (pool de threads de FastAPI)."""
        from concurrent.futures import ThreadPoolExecutor
        paths = ["/state", "/models", "/vehicles", "/drivers", "/lines", "/report", "/events"] * 6
        with ThreadPoolExecutor(max_workers=8) as pool:
            codes = list(pool.map(lambda p: self.client.get("/api" + p).status_code, paths))
        self.assertEqual(set(codes), {200})

    def test_index_page_and_static(self):
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertEqual(self.client.get("/static/app.js").status_code, 200)
        self.assertEqual(self.client.get("/static/style.css").status_code, 200)

    def test_tick_then_report(self):
        self.assertEqual(self.get("/report")["day"], None)
        r = self.client.post("/api/tick", json={"days": 3})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.json()["days"]), 3)
        rep = self.get("/report")
        self.assertEqual(rep["day"], 2)
        self.assertEqual(len(rep["history"]), 3)
        self.assertTrue(rep["ledger"])
        self.assertEqual(self.get("/state")["game_day"], 3)

    def test_tick_disabled_outside_dev(self):
        client = TestClient(create_app(os.path.join(self.tmp.name, "prod.db"), dev=False))
        self.assertEqual(client.post("/api/tick", json={"days": 1}).status_code, 403)

    def test_buy_vehicle_and_errors_are_400(self):
        models = self.get("/models")
        cheapest = models[0]
        r = self.client.post("/api/vehicles", json={"model_id": cheapest["id"]})
        self.assertEqual(r.status_code, 201, r.text)
        self.assertEqual(len(self.get("/vehicles")), 2)
        expensive = models[-1]
        r = self.client.post("/api/vehicles", json={"model_id": expensive["id"]})
        self.assertEqual(r.status_code, 400)
        self.assertIn("Trésorerie", r.json()["detail"])

    def test_hire_and_service_workflow(self):
        d = self.client.post("/api/drivers", json={"name": "Alice"}).json()["id"]
        line = self.get("/lines")[1]  # L2 : bus standard uniquement
        s = self.client.post("/api/services", json={"line_id": line["id"], "start_hour": 6, "end_hour": 10,
                                                    "day_type": "weekday"})
        self.assertEqual(s.status_code, 201)
        sid = s.json()["id"]
        vid = self.get("/vehicles")[0]["id"]
        # Le bus de départ est déjà pris 5h-14h et 14h-23h sur L1 : conflit
        r = self.client.patch(f"/api/services/{sid}", json={"vehicle_id": vid})
        self.assertEqual(r.status_code, 400)
        r = self.client.patch(f"/api/services/{sid}", json={"driver_id": d})
        self.assertEqual(r.status_code, 200)
        l2 = [x for x in self.get("/lines") if x["id"] == line["id"]][0]
        self.assertEqual(l2["services"][0]["driver_name"], "Alice")
        self.assertEqual(self.client.patch(f"/api/services/{sid}", json={"unassign_driver": True}).status_code, 200)
        self.assertEqual(self.client.delete(f"/api/services/{sid}").status_code, 200)
        self.assertEqual(self.client.delete(f"/api/services/{sid}").status_code, 400)

    def test_validation_errors(self):
        self.assertEqual(self.client.post("/api/drivers", json={"name": ""}).status_code, 422)
        self.assertEqual(self.client.post("/api/tick", json={"days": 0}).status_code, 422)
        self.assertEqual(self.client.post("/api/vehicles/1/inconnue").status_code, 404)

    def test_vehicle_actions(self):
        vid = self.get("/vehicles")[0]["id"]
        self.assertEqual(self.client.post(f"/api/vehicles/{vid}/ct").status_code, 200)
        self.assertEqual(self.client.post(f"/api/vehicles/{vid}/revision").status_code, 200)
        r = self.client.post(f"/api/vehicles/{vid}/overhaul")  # déjà à l'atelier
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.get("/vehicles")[0]["status"], "workshop")


if __name__ == "__main__":
    unittest.main()
