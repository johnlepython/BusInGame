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
        status = self.run_cli("status")
        self.assertIn("Ma Compagnie", status)
        self.assertIn("diesel", status)
        self.assertIn("Valideur", status)
        self.assertIn("j2:", self.run_cli("tick", "--days", "3"))

    def test_new_with_player_account(self):
        self.run_cli("new", "--user", "bob", "--password", "motdepasse1")
        conn = connect(self.db)
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM company c JOIN account a ON a.id = c.account_id WHERE a.username = 'bob'").fetchone()[0], 1)
        with self.assertRaises(SystemExit):
            self.run_cli("new", "--user", "bob", "--password", "motdepasse1")  # identifiant déjà pris

    def test_tick_to_today_catches_up_all_companies_then_is_a_noop(self):
        conn = connect(self.db)
        init_db(conn)
        seed.new_game(conn, start_date=date.today() - timedelta(days=4))
        seed.new_game(conn, start_date=date.today() - timedelta(days=1))
        conn.close()
        out = self.run_cli("tick", "--to-today")
        self.assertIn("7 jour(s) calculé(s) pour 2 compagnie(s)", out)   # 5 + 2
        self.assertIn("Déjà à jour", self.run_cli("tick", "--to-today"))

    def test_balance_runs(self):
        out = self.run_cli("balance")
        self.assertIn("Ligne", out)
        self.assertIn("retour sur investissement", out)
        self.assertIn("Réseau complet", out)

    def test_simulate_runs(self):
        out = self.run_cli("simulate", "--days", "30")
        self.assertIn("Fin au jour 30", out)


if __name__ == "__main__":
    unittest.main()
