"""Interface en ligne de commande.

  python -m busmanager new                  # crée game.db avec une nouvelle partie
  python -m busmanager status               # état de la compagnie
  python -m busmanager tick --days 7        # avance de 7 jours
  python -m busmanager simulate --days 200  # simulation d'équilibrage (en mémoire)
"""
import argparse

from . import engine, seed, sim
from .db import connect, init_db


def _company_id(conn):
    row = conn.execute("SELECT id FROM company ORDER BY id LIMIT 1").fetchone()
    if row is None:
        raise SystemExit("Aucune partie : lance d'abord `python -m busmanager new`.")
    return row["id"]


def cmd_new(args):
    conn = connect(args.db)
    init_db(conn)
    cid = seed.new_game(conn, name=args.name, seed=args.seed)
    print(f"Partie créée dans {args.db} (compagnie {cid}).")


def cmd_status(args):
    conn = connect(args.db)
    cid = _company_id(conn)
    c = conn.execute("SELECT * FROM company WHERE id = ?", (cid,)).fetchone()
    print(f"{c['name']} — jour {c['game_day']} — trésorerie {c['cash']:,.0f} € — réputation {c['reputation']:.1f}")
    print("\nVéhicules :")
    for v in conn.execute(
        """SELECT v.*, m.name AS model FROM vehicle v JOIN bus_model m ON m.id = v.model_id
           WHERE v.company_id = ? ORDER BY v.id""", (cid,)):
        print(f"  {v['plate']}  {v['model']:<26} {v['km']:>9,.0f} km  moteur {v['engine_state']:5.1f}  "
              f"intérieur {v['interior_state']:5.1f}  {v['status']}  "
              f"révision j{v['revision_due_day']}  CT j{v['ct_due_day']}")
    last = conn.execute(
        "SELECT * FROM tick_log WHERE company_id = ? ORDER BY day DESC LIMIT 1", (cid,)
    ).fetchone()
    if last:
        print(f"\nDernier jour calculé : j{last['day']} — résultat {last['cash_after'] - last['cash_before']:+,.0f} €")
        for r in conn.execute(
            "SELECT kind, SUM(amount) AS total FROM ledger WHERE company_id = ? AND day = ? GROUP BY kind",
            (cid, last["day"])):
            print(f"  {r['kind']:<12} {r['total']:>10,.0f} €")
    print("\nDerniers événements :")
    for e in conn.execute(
        "SELECT * FROM event_log WHERE company_id = ? ORDER BY id DESC LIMIT 5", (cid,)):
        print(f"  j{e['day']}: {e['message']}")


def cmd_tick(args):
    conn = connect(args.db)
    cid = _company_id(conn)
    start = conn.execute("SELECT game_day FROM company WHERE id = ?", (cid,)).fetchone()["game_day"]
    for r in engine.catch_up(conn, cid, start + args.days):
        print(f"j{r['day']}: {r['profit']:+,.0f} € (trésorerie {r['cash_after']:,.0f} €)")


def cmd_simulate(args):
    result = sim.simulate(days=args.days, seed_value=args.seed)
    result["conn"].close()
    print(f"\nFin au jour {result['day']} : trésorerie {result['cash']:,.0f} €, réputation {result['reputation']:.1f}")


def main(argv=None):
    p = argparse.ArgumentParser(prog="busmanager")
    p.add_argument("--db", default="game.db")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("new"); s.add_argument("--name", default="Ma Compagnie"); s.add_argument("--seed", type=int, default=1)
    s.set_defaults(func=cmd_new)
    s = sub.add_parser("status"); s.set_defaults(func=cmd_status)
    s = sub.add_parser("tick"); s.add_argument("--days", type=int, default=1); s.set_defaults(func=cmd_tick)
    s = sub.add_parser("simulate"); s.add_argument("--days", type=int, default=200); s.add_argument("--seed", type=int, default=1)
    s.set_defaults(func=cmd_simulate)

    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
