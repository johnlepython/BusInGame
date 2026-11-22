# Bus Manager

Jeu web de gestion d'un réseau de bus. Ce dépôt contient pour l'instant le **moteur du niveau 1** (sans interface web) :
base SQLite, règles, actions du joueur, tick quotidien, simulation d'équilibrage.

Python 3.10+, **aucune dépendance** (bibliothèque standard uniquement).

## Utilisation

```bash
python3 -m busmanager new                 # crée game.db avec une partie de départ
python3 -m busmanager status              # état de la compagnie, du parc, dernier bilan
python3 -m busmanager tick --days 7       # avance de 7 jours
python3 -m busmanager simulate --days 360 # simulation d'équilibrage (en mémoire, rien n'est écrit)
python3 -m unittest discover -s tests     # tests
```

## Organisation

| Fichier | Rôle |
|---|---|
| `busmanager/schema.sql` | Tables : compagnie, modèles de bus, véhicules, dépôts, conducteurs, lignes, demande horaire, services, ledger, rapports, événements |
| `busmanager/rules.py` | **Toutes les constantes d'équilibrage** (prix énergie, usure, pannes, salaires…) |
| `busmanager/actions.py` | Actions du joueur avec validations (achat, embauche, services, affectations, révision, CT) |
| `busmanager/engine.py` | Tick quotidien : demande → passagers servis → recettes → coûts → usure → pannes → réputation |
| `busmanager/seed.py` | Catalogue de bus, lignes de départ, courbes de demande |
| `busmanager/sim.py` | Stratégie gloutonne pour tester l'économie sur de longues durées |
| `busmanager/cli.py` | Commandes `new`, `status`, `tick`, `simulate` |

## Principes

- **Tick idempotent** : `tick_log` empêche de calculer deux fois le même jour. `catch_up(conn, company, jour_cible)` rattrape les jours manquants, donc un cron en retard ou lancé deux fois est sans danger.
- **Reproductible** : les pannes viennent d'un hasard dérivé de `(graine, compagnie, jour)`.
- **Comptabilité cohérente** : `company.cash` = somme du `ledger`. Tout mouvement d'argent passe par `actions.post()`.
- **Un service** = une plage horaire sur une ligne, avec un bus et un conducteur. Un conducteur a 9 h de conduite maximum par jour.

## État actuel et limites connues

- **L'économie n'est pas équilibrée.** Le bus de départ rapporte environ 3 000 €/jour, soit un amortissement en un mois. Les valeurs de `rules.py` et `seed.py` sont des points de départ ; c'est le rôle de `simulate` de les régler.
- Les pannes sont tirées en fin de journée (le bus est indisponible dès le lendemain).
- Le carburant est un simple coût au kilomètre : pas encore de citernes, stocks ni bornes de recharge.
- Pas de stock d'options, de contrats publicitaires, de plannings détaillés, ni d'interface web.

## Suite prévue

1. Équilibrer l'économie (revenus, prix des bus, demande) avec `simulate`.
2. API web (FastAPI) et écrans de gestion.
3. Niveau 2 : ateliers, énergies et infrastructures, stock d'options, contrats publicitaires.
