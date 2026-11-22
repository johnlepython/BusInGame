# Bus Manager

Jeu web de gestion d'un réseau de bus, inspiré de Transport Manager. Les « niveaux » sont les niveaux du jeu :
le **niveau 1** (implémenté) contient les comptes joueurs, un réseau de 4 lignes, un parc de bus (neufs et d'occasion),
des conducteurs et des services, un dépôt avec citernes et bornes de recharge, et un atelier avec stock d'options
(valideurs). Un script calcule les gains chaque jour.

## Démarrage rapide

```bash
pip install -r requirements.txt            # seulement pour l'API web (le moteur et le CLI n'ont aucune dépendance)
uvicorn busmanager.api:app --reload        # puis ouvrir http://127.0.0.1:8000
```

La base `game.db` est créée au premier lancement. **Si tu as une ancienne `game.db`, supprime-la** : le schéma a changé.
Sur l'écran de connexion, « Créer un compte » crée un identifiant, un mot de passe et une compagnie de départ.
Le bouton « Passer au jour suivant » du tableau de bord n'existe qu'en mode développement (`BUSMANAGER_DEV=0` le désactive).

Variables d'environnement : `BUSMANAGER_DB` (fichier SQLite), `BUSMANAGER_DEV`, `BUSMANAGER_SECURE_COOKIES=1`
(cookie de session « Secure », obligatoire derrière HTTPS).

## En ligne de commande

```bash
python3 -m busmanager new                                   # partie sans compte joueur dans game.db
python3 -m busmanager new --user bob --password motdepasse  # partie rattachée à un compte
python3 -m busmanager status                                # compagnie, dépôt, parc, dernier bilan
python3 -m busmanager tick --days 7                         # avance la 1re compagnie de 7 jours
python3 -m busmanager tick --to-today                       # rattrape TOUTES les compagnies jusqu'à aujourd'hui inclus
python3 -m busmanager balance                               # analyse théorique des lignes
python3 -m busmanager simulate --days 360                   # simulation automatique en mémoire
python3 -m unittest discover -s tests                       # tests (API ignorés si fastapi absent)
```

### Calcul quotidien automatique

Le jour de jeu 0 est la date de création de la compagnie : un jour de jeu = un jour réel, et le type de jour
(semaine, samedi, dimanche) suit le vrai calendrier. Le cron lance chaque soir `tick --to-today`, qui rattrape tous
les jours manquants ; il est sans danger s'il tourne en retard ou deux fois (un jour déjà calculé n'est jamais refait).

```cron
55 23 * * *  cd /chemin/bus-manager && python3 -m busmanager tick --to-today
```

## Règles du niveau 1

- **Lignes** : durée, distance, tarif, carrosseries autorisées (standard / articulé / midi), **fréquence cible** en départs
  par heure et par sens, et demande de voyageurs par heure, selon le type de jour (semaine, samedi, dimanche) et la
  tranche (creuse, pointe, soir). Un bus assure `30 / durée` départs par heure et par sens : tenir une fréquence f demande
  `f × durée / 30` bus.
- **Services** : plage horaire sur une ligne, tenue par un bus et un conducteur (9 h de conduite maximum par jour), pour
  tous les jours, la semaine, le samedi, le dimanche ou le week-end.
- **Recettes** : voyageurs transportés (demande × effet de réputation, limitée par la capacité) × tarif.
  Un bus sans valideur perd 10 % de ses recettes en fraude.
- **Énergie** : diesel/hybride/GNV pris dans la citerne du dépôt ; si elle est vide ou absente, plein en station publique
  (+30 % diesel/hybride, +40 % GNV). Les bus électriques ont une autonomie journalière (batterie rechargée la nuit) et
  exigent une place de borne (3 bus par borne), sinon ils ne roulent pas.
- **Atelier** : révision (1 j), révision générale du moteur, remise à neuf de l'intérieur, contrôle technique. Les valideurs
  s'achètent pour le stock puis s'installent : le bus est immobilisé 1 jour. Un bus à l'atelier ne roule pas.
- **Usure et pannes** : usure du moteur au km, de l'intérieur par passager ; probabilité de panne selon la fiabilité,
  l'état du moteur et le retard de révision. Les pannes sont tirées avec un hasard reproductible (graine).
- **Réputation** (0–100) : 40 % passagers transportés, 30 % fréquence tenue, 30 % état du parc ; elle module la demande.
- **Comptabilité** : en caisse, chaque mouvement est écrit au `ledger` (invariant : trésorerie = somme du ledger).

## Données (dossier `data/`)

Tout est modifiable sans toucher au code ; une erreur de format est signalée avec le nom de la ligne ou du modèle fautif.

- `bus_models.json` : liste de modèles `{brand, model, body, status ("neuf"|"occasion"), capacity, [initial], motors}`.
  Chaque motorisation : `energy` (diesel, gnv, hybride, electrique), `consumption_per_100km` (L, kg ou kWh), `price`,
  et `range_km` pour l'électrique. `initial` (occasions) : `km`, `engine_state`, `interior_state`, `ct_days`, `revision_days`.
  Fiabilité et coût d'entretien ont des valeurs par défaut, surchargeables par motorisation.
- `lines.json` : `code`, `name`, `terminus`, `duration_min`, `distance_km`, `allowed_bodies`, `fare`, `frequency` et
  `demand_per_hour` (`weekday|saturday|sunday` × `creuse|pointe|soir`) ; ou `hourly` pour une demande heure par heure.
- `options.json` : options achetables (`code`, `name`, `price`, `install_days`, `description`).
- `game.json` : trésorerie de départ (une fois la flotte achetée), places du dépôt, citernes et stock de départ, stock
  d'options, flotte de départ (`model`, `line`, `options`).

Les prix, fréquences, demandes et états des occasions fournis sont **inventés** pour équilibrer le jeu.

## Équilibrage

`python3 -m busmanager balance` estime, ligne par ligne, les bus nécessaires pour tenir les fréquences, les recettes, les
coûts et le temps de retour sur investissement. `simulate` joue une partie complète avec une stratégie simple
(entretien à temps, valideurs, citerne remplie, achat de bus là où la fréquence manque). Cible : retour sur
investissement de 150 à 300 jours, économie positive sur 360 jours.

## Comptes et sécurité

Mots de passe hachés avec scrypt ; le jeton de session (cookie `httponly`, `SameSite=Lax`, 30 jours) n'est jamais stocké,
seule son empreinte SHA-256 l'est. Chaque route vérifie que l'objet appartient à la compagnie du joueur connecté.
L'authentification reste basique : pas de réinitialisation de mot de passe ni de limitation de tentatives. À durcir (HTTPS,
limitation de débit) avant toute exposition publique.

## Structure

```
busmanager/   rules.py (constantes), schema.sql, seed.py (données), actions.py (actions du joueur),
              engine.py (calcul quotidien), auth.py, api.py (FastAPI), cli.py, balance.py, sim.py
static/       interface (HTML/JS/CSS sans étape de build)
data/         catalogue, lignes, options, partie de départ
tests/        unittest
```
