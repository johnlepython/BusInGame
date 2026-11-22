import contextlib
import io
import os
import tempfile
import unittest
from datetime import date, timedelta

from busmanager import cli, seed
from busmanager.db import connect, init_db


class CliTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = os.path.join(tmp.name, "g.db")

    def run_cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.main(["--db", self.db, *argv])
        return out.getvalue()

    def test_new_status_tick(self):
        self.run_cli("new")
        self.assertIn("Ma Compagnie", self.run_cli("status"))
        self.assertIn("j2:", self.run_cli("tick", "--days", "3"))

    def test_tick_to_today_catches_up_then_is_a_noop(self):
        conn = connect(self.db)
        init_db(conn)
        seed.new_game(conn, start_date=date.today() - timedelta(days=4))
        conn.close()
        out = self.run_cli("tick", "--to-today")
        self.assertEqual(out.count("\nj") + (1 if out.startswith("j") else 0), 5)  # 5 jours : J-4 .. aujourd'hui
        self.assertIn("Déjà à jour", self.run_cli("tick", "--to-today"))

    def test_balance_runs(self):
        out = self.run_cli("balance")
        self.assertIn("L1", out)
        self.assertIn("Retour", out)


if __name__ == "__main__":
    unittest.main()
