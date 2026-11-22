# Bus Manager

Jeu web de gestion d'un réseau de bus. Niveau 1 : moteur, API et interface de gestion jouable.

## Démarrage rapide

```bash
pip install -r requirements.txt            # seulement pour l'API web (le moteur et le CLI n'ont aucune dépendance)
uvicorn busmanager.api:app --reload        # puis ouvrir http://127.0.0.1:8000
```

Au premier lancement, la base `game.db` et une partie de départ sont créées automatiquement
(pour repartir de zéro : supprimer `game.db`). Le bouton « Passer au jour suivant » du tableau de bord
fait avancer le jeu en mode développement (`BUSMANAGER_DEV=0` le désactive).

## En ligne de commande

```bash
python3 -m busmanager new                  # nouvelle partie dans game.db
python3 -m busmanager status               # état de la compagnie, du parc, dernier bilan
python3 -m busmanager tick --days 7        # avance de 7 jours
python3 -m busmanager tick --to-today      # rattrape tous les jours jusqu'à aujourd'hui inclus
python3 -m busmanager balance              # analyse théorique de chaque ligne (voir « Équilibrage »)
python3 -m busmanager simulate --days 360  # simulation automatique, en mémoire
python3 -m unittest discover -s tests      # tests (les tests API sont ignorés si fastapi n'est pas installé)
```

### Calcul quotidien automatique

Le jour de jeu 0 correspond à la date de création de la partie : **un jour de jeu = un jour réel**, et le type de jour
(semaine ou week-end : samedi et dimanche) suit le vrai calendrier. Pour le calcul quotidien, lancer chaque soir :

```cron
55 23 * * *  cd /chemin/vers/bus-manager && python3 -m busmanager tick --to-today
```

`--to-today` est idempotent : s'il est lancé en retard ou deux fois, il rattrape les jours manquants sans jamais
recalculer un jour déjà fait.

## Vos données : lignes et catalogue de bus

Tout se règle dans `data/` (aucun code à modifier). Une nouvelle partie recharge ces fichiers.

**`data/bus_models.json`** : liste de modèles de bus

```json
{"name": "Standard 12 m diesel", "energy": "diesel", "body": "standard", "capacity": 90,
 "consumption_per_100km": 38.0, "price": 95000, "reliability": 0.985, "maintenance_cost_per_km": 0.30}
```

`energy` : `diesel` (L), `gnv` (kg) ou `electrique` (kWh) pour la consommation. `body` : `standard` ou `articule`.
`reliability` : entre 0 et 1, plus haut = moins de pannes.

**`data/lines.json`** : liste de lignes

```json
{"code": "L1", "name": "Centre-ville ↔ Université", "distance_km": 6.0, "duration_min": 25,
 "allowed_body": "any", "fare": 0.7, "weekday_passengers": 3200, "weekend_factor": 0.55}
```

- `distance_km` et `duration_min` : pour **un trajet** (sens unique).
- `allowed_body` : `any` ou `standard` (interdit les articulés).
- `fare` : recette moyenne par passager (après tarifs réduits, abonnements, etc.).
- Demande : soit `weekday_passengers` + `weekend_factor` (réparti selon un profil horaire : `profile_weekday` / `profile_weekend`,
  au choix `commuter` ou `leisure`), soit une demande explicite par heure :
  `"hourly": {"weekday": {"7": 600, "8": 700}, "weekend": {"11": 300}}`.

Les erreurs de format sont signalées avec le nom du champ ou du modèle concerné.

## Équilibrage

`python3 -m busmanager balance` estime pour chaque ligne et chaque type de jour : le nombre de bus nécessaires, les recettes,
les coûts et le **retour sur investissement**. Repère : entre 100 et 300 jours, le jeu est intéressant ; en dessous, c'est trop
facile ; « jamais » signale une ligne ruineuse. Les constantes (salaires, usure, pannes, prix de l'énergie…) sont dans
`busmanager/rules.py`. `simulate` joue une année avec une stratégie simple pour vérifier que l'économie ne s'effondre ni n'explose.

## Règles de jeu (niveau 1)

- Un **service** = une plage horaire sur une ligne (tous les jours, semaine ou week-end), tenue par un bus et un conducteur.
  Un conducteur a 9 h de conduite maximum par jour ; ni un bus ni un conducteur ne peuvent avoir deux services qui se chevauchent.
- Un bus sans **contrôle technique** valide ne roule pas. Une **révision** en retard augmente le risque de panne.
- **Ateliers** : révision (+25 d'état moteur, 1 jour), révision générale du moteur (état à 100, 4 jours), remise à neuf de l'intérieur (état à 100, 2 jours).
  Un bus à l'atelier ne roule pas. Une panne l'immobilise 1 à 4 jours.
- **Réputation** : dépend du taux de passagers transportés et de l'état du parc roulant ; elle module la demande (de ×0,7 à ×1,3).
- Les salaires sont payés chaque jour à tous les conducteurs, qu'ils roulent ou non ; le dépôt coûte une somme fixe par place.

## Organisation du code

| Fichier | Rôle |
|---|---|
| `busmanager/schema.sql` | Tables SQLite |
| `busmanager/rules.py` | Constantes d'équilibrage et petites formules |
| `busmanager/seed.py` | Chargement et validation des JSON, création d'une partie |
| `busmanager/actions.py` | Actions du joueur avec validations (achat, embauche, services, affectations, ateliers) |
| `busmanager/engine.py` | Tick quotidien (`run_day`), rattrapage (`catch_up`) |
| `busmanager/balance.py` | Analyse théorique des lignes |
| `busmanager/sim.py` | Stratégie gloutonne de simulation |
| `busmanager/api.py` | API FastAPI + service de l'interface |
| `busmanager/cli.py` | Commandes `new`, `status`, `tick`, `balance`, `simulate` |
| `static/` | Interface (HTML, CSS, JavaScript sans build) |

Principes : le tick est **idempotent** (`tick_log`) et **reproductible** (le hasard des pannes dérive de la graine, de la
compagnie et du jour) ; `company.cash` est toujours égal à la somme du `ledger` (tout mouvement passe par `actions.post()`).

## Limites actuelles

- Une seule compagnie, pas de comptes joueurs ni d'authentification (l'API n'est pas faite pour être exposée telle quelle).
- Le carburant est un coût au kilomètre : pas encore de citernes, de stocks ni de bornes de recharge.
- Pas de stock d'options, de contrats publicitaires, de services spéciaux, de parcs relais ni de gare routière (niveau 2).
- Les pannes sont tirées en fin de journée (le bus est indisponible dès le lendemain).
- Les valeurs de `data/` sont des exemples ; l'économie a été réglée pour elles, pas pour vos lignes.
