# Pipeline de suivi de champ — comptes, parcelles, NDVI/NDRE via Sentinel-2

Code réel du projet (pas des mockups) : gestion de compte, enregistrement de
parcelles, récupération (ou simulation) de bandes Sentinel-2, calcul NDVI et
NDRE, découpage en zones, détection d'alertes, export JSON + cartes PNG.

## Priorités réelles du projet

Pour éviter de disperser l'effort, les priorités du moment sont les suivantes :

1. **Stabiliser le cœur métier** : inscription/connexion, création de champ,
   validation de géométrie, stockage et lecture cohérents.
2. **Valider la qualité des résultats** : indices et zonage sur des données
   réelles ou au moins des données de démo réalistes, sans se disperser sur
   des sujets trop éloignés du cœur du produit.
3. **Rendre l'API plus sûre et plus robuste** : secrets via variables
   d'environnement, CORS maîtrisé, tests de régression autour des cas limites.
4. **Élargir la couverture de tests** sur la validation métier et les sorties
   de traitement, avant de penser à des améliorations plus "cosmétiques".

Les sujets suivants restent utiles, mais sont **secondaires** tant que le
cœur de l'application n'est pas stabilisé : PWA, déploiement multiplateforme,
app native, ou stockage de données plus avancé.

## Structure

```
src/
  db.py                → schéma SQLite (users, fields)
  auth.py              → création de compte, connexion (PBKDF2)
  fields.py            → enregistrement des parcelles, calcul de surface
  api.py               → API FastAPI (endpoints HTTP réels, protégés par JWT)
  tokens.py            → émission / vérification des tokens JWT de session
  test_api.py           → tests de l'API via de vraies requêtes HTTP
  fetch_sentinel.py    → appel réel à l'API Sentinel Hub (nécessite une clé)
  indices.py           → calcul NDVI / NDRE à partir des bandes brutes
  zoning.py            → découpage en grille + classification (ok/warning/critical)
  zoning_kmeans.py     → découpage en zones homogènes réelles (k-means)
  compare_zoning.py    → compare visuellement grille vs k-means sur les mêmes données
  demo_data.py         → génère des bandes synthétiques (pour tester sans réseau)
  main.py              → pipeline indices/zones, en ligne de commande
  demo_flow.py         → démo bout-en-bout : compte → champ → pipeline (sans API)
output/
  demo.db              → base SQLite générée par demo_flow.py
  zones.json           → données de zones (sortie de main.py)
  zones_field_1.json   → données de zones liées à un champ précis
  carte_ndvi.png / carte_ndre.png
static/                → servi directement par l'API (voir "Installer l'app")
  onboarding_mobile.html
  apercu_champ_mobile.html
  manifest.json         → manifest PWA (nom, icônes, couleurs)
  service-worker.js     → requis pour l'installabilité Android/Chrome
  icon-192.png / icon-512.png
generate_icons.py      → régénère les icônes de static/ si besoin
```

## Installation

```bash
pip install -r requirements.txt
```

Configuration locale minimale :

```bash
cp .env.example .env    # contient ENV=development (secret JWT de dev autorisé)
```

Lancer les tests : `python -m pytest tests`

## Utilisation

⚠️ **Toutes les commandes ci-dessous s'exécutent depuis la racine du projet
(`champ_app/`, le dossier qui contient `src/` et `requirements.txt`), pas
depuis l'intérieur de `src/`.** Les modules s'importent entre eux via
`from src.xxx import ...`, donc Python doit être lancé avec `-m` (mode
module) plutôt que directement sur le fichier — `python -m src.main` et non
`python src/main.py`, sans quoi Python ne retrouve pas les modules
(`ModuleNotFoundError: No module named 'src'`).

**Lancer l'API (serveur HTTP réel)** :
```bash
uvicorn src.api:app --reload --port 8000
```
Documentation interactive auto-générée : http://localhost:8000/docs

Endpoints disponibles :
| Méthode | Route | Auth requise | Rôle |
|---|---|---|---|
| POST | `/auth/register` | non | Créer un compte, renvoie un token JWT et sa durée de validité |
| POST | `/auth/login` | non | Se connecter, renvoie un token JWT et sa durée de validité |
| POST | `/fields` | oui | Enregistrer un champ (rattaché à l'utilisateur du token) |
| GET | `/fields` | oui | Lister les champs de l'utilisateur du token |
| POST | `/fields/{field_id}/analyses` | oui | Lancer et enregistrer une analyse de zones (démo ou Sentinel) |
| GET | `/fields/{field_id}/analyses` | oui | Consulter l'historique des analyses du champ |
| POST | `/fields/{field_id}/treatments` | oui | Enregistrer le traitement d'une zone d'analyse |
| GET | `/fields/{field_id}/treatments` | oui | Consulter les traitements enregistrés du champ |
| GET | `/fields/{field_id}/zones` | oui | Zones NDVI/NDRE d'un champ (`?method=grid` ou `kmeans`, avec `n_rows`, `n_cols`, `n_zones` optionnels, 404 si pas le sien) |
| GET | `/crops` | non | Liste des cultures reconnues |

L'authentification se fait via l'en-tête `Authorization: Bearer <token>`, token obtenu à l'inscription ou la connexion.

Une analyse est maintenant conservée avec sa date, ses paramètres de zonage
et son résultat. `POST /fields/{field_id}/analyses` accepte les mêmes options
de zonage que la route `/zones` (`method`, `n_rows`, `n_cols`, `n_zones`,
`critical_threshold`, `warning_threshold`). La source est explicite :
`synthetic` par défaut pour la démo, ou `sentinel` pour une récupération
réelle avec `SH_CLIENT_ID` et `SH_CLIENT_SECRET`. Les options `acquisition_date`
(`YYYY-MM-DD`) et `resolution` (mètres, 10 par défaut) sont disponibles en
mode Sentinel ; une erreur de récupération est renvoyée sans substituer de
données de démo.

L'écran `onboarding_mobile.html` ouvre `apercu_champ_mobile.html` pour une
parcelle connectée. Cet écran charge en priorité la dernière analyse K-means
enregistrée, puis le bouton **Actualiser l'analyse** crée une nouvelle analyse
de démonstration et rafraîchit la carte. L'intégration d'une action utilisateur
pour lancer une analyse Sentinel reste à concevoir avant d'exposer cette option
dans l'interface.

Le bouton **Marquer cette zone comme traitée** enregistre désormais la zone et
l'analyse affichée. Il n'est disponible qu'après le chargement d'une analyse
persistée, afin de conserver une trace fiable de l'intervention.

La session mobile est conservée dans `localStorage` et le token JWT n'est pas
placé dans l'URL de la parcelle, afin qu'il ne soit pas exposé par l'historique,
les journaux ou un partage de lien.

**Tester l'API sans lancer de serveur (TestClient)** :
```bash
python -m src.test_api
```

**Démo bout-en-bout sans passer par l'API (compte + champ + pipeline)** :
```bash
python -m src.demo_flow
```

**Juste le pipeline en mode démo** :
```bash
python -m src.main --demo
```

**Mode réel Sentinel Hub (nécessite un compte gratuit sur sentinel-hub.com)** :
```bash
export SH_CLIENT_ID="votre_id"
export SH_CLIENT_SECRET="votre_secret"
python -m src.main --bbox 2.30,48.85,2.35,48.90
```

## Ce qui est réellement testé vs. ce qui ne l'est pas

- **Testé et fonctionnel** (exécuté dans cet environnement, résultats vérifiés) :
  - **l'API FastAPI en entier**, via de vraies requêtes HTTP (`test_api.py`) :
    inscription, doublon d'email rejeté (400), connexion, mauvais mot de
    passe rejeté (401), accès sans token rejeté (401), token invalide
    rejeté (401), création de champ, culture invalide rejetée (422), liste
    des champs, récupération des zones NDVI/NDRE, **et isolation entre
    utilisateurs vérifiée** (un utilisateur ne peut pas voir le champ d'un
    autre — 404)
  - **le module `tokens.py`** : création/décodage de token, rejet d'un
    token invalide, rejet d'un token expiré (testé avec un token expiré
    fabriqué manuellement)
  - création de compte avec validation (email, mot de passe ≥ 8 caractères,
    unicité) et rejet propre des cas invalides
  - connexion avec vérification du mot de passe haché (PBKDF2, pas de mot de
    passe stocké en clair)
  - enregistrement d'un champ avec validation (culture dans une liste
    connue, bbox géographiquement cohérente) et calcul automatique de la
    surface en hectares à partir des coordonnées
  - le calcul des indices, le découpage en zones, la classification, l'export
    JSON, et la génération des cartes — sur données synthétiques réalistes
- **Non testé ici** : l'appel réel à Sentinel Hub (`fetch_sentinel.py`).
  Le code suit l'API officielle `sentinelhub-py`, mais je n'ai pas pu
  l'exécuter dans cet environnement (pas d'accès réseau vers
  `services.sentinel-hub.com`). À tester avec vos propres identifiants.

## Contour réel de la parcelle (polygone) plutôt qu'un simple rectangle

`register_field()` accepte maintenant soit une `bbox` (comme avant, pour
compatibilité), soit un `polygon` réel — une liste de points `(lon, lat)`.
Quand un polygone est fourni :
- la bbox est dérivée automatiquement (utile pour la requête satellite, qui
  a besoin d'un rectangle englobant)
- la surface est calculée sur la vraie forme (formule du lacet sur une
  projection équirectangulaire locale), pas sur le rectangle englobant —
  testé sur une forme en L : 13.519 ha réels contre 20+ ha si on avait
  utilisé la bbox englobante
- le polygone est stocké tel quel (`polygon_geojson` en base) et renvoyé
  par l'API pour être réaffiché

**Écran mobile correspondant** : `onboarding_mobile.html` utilise
maintenant une vraie carte (Leaflet + OpenStreetMap) avec un outil de
dessin de polygone (Leaflet.draw) au lieu d'un rectangle simulé sur un
dégradé. La formule de calcul de surface côté client (JavaScript) est
volontairement identique à celle du serveur (Python) — **vérifié** : les
deux donnent 13.519 ha sur la même forme en L, testés indépendamment.

## Deux méthodes de segmentation, et pourquoi ça compte

- **`grid`** (défaut) : grille régulière fixe (8x12 = 96 zones). Rapide,
  simple, mais arbitraire — une zone de grille peut couper en deux une
  vraie zone de stress, ou au contraire regrouper des sous-parties très
  différentes.
- **`kmeans`** : `n_zones` zones homogènes réelles, calculées par clustering
  sur la valeur d'indice + la position spatiale. Sur les données de test
  (`compare_zoning.py`), ça change concrètement le résultat : la même
  parcelle synthétique donne **8 zones d'alerte éparpillées** en mode
  grille contre **1 seule zone bien délimitée** en mode k-means — beaucoup
  plus proche de ce qu'un agronome tracerait à la main pour une
  recommandation d'intervention ciblée.

Comparer les deux : `python -m src.compare_zoning` (génère une image
côte-à-côte dans `output/comparaison_grille_vs_kmeans.png`).

## Priorité secondaire — PWA et déploiement

Le projet contient déjà une base de **PWA** : les fichiers de l'interface
mobile sont servis par l'API, et l'app peut être installée sur un téléphone
via un navigateur moderne. Cela reste utile, mais ce n'est pas la priorité
majeure tant que le cœur métier n'est pas pleinement stabilisé.

À garder en vue, mais sans faire perdre de temps maintenant :
- `static/manifest.json` et `static/service-worker.js`
- un déploiement HTTPS propre pour pouvoir installer l'app sur mobile
- une évolution vers une vraie app native seulement si l'usage métier le
  justifie réellement

## Fiabilité des analyses et limites d'usage

**Ce qui est calculé, et sur quels pixels**
- Seuls les pixels **à l'intérieur du contour** de la parcelle (`polygon`) sont
  utilisés ; routes, voisins et bois de la bbox sont ignorés (`src/geometry.py`).
- Les **nuages et ombres** sont exclus par la bande de classification `SCL` de
  Sentinel-2 L2A (seules les classes 4 « végétation » et 5 « sol nu » sont
  gardées). `dataMask` seul ne détecte pas les nuages. ⚠️ Non testé contre le vrai
  service : à valider sur des images réelles.
- Chaque analyse contient `result.data_quality` (`valid_pixel_ratio`,
  `low_quality`). Sous 60 % de pixels exploitables l'analyse est signalée comme peu
  fiable ; sous 20 % elle est refusée (422).
- Les seuils `critical` / `warning` doivent être finis, compris entre -1 et 1, avec
  `critical < warning`, et sont appliqués par les deux méthodes (grille et k-means).
- ⚠️ Les seuils par défaut (0,35 / 0,55) sont uniques pour toutes les cultures et
  tous les stades : **ils doivent être validés par un agronome** avant tout usage réel.

**Limites (protection contre les abus et les coûts Sentinel Hub)**

| Limite | Valeur | Réglage |
|---|---|---|
| Sommets d'un polygone | 500 | `MAX_POLYGON_VERTICES` (`fields.py`) |
| Surface d'une parcelle / de son rectangle englobant | 1 000 ha / 4 000 ha | `MAX_FIELD_AREA_HA`, `MAX_BBOX_AREA_HA` |
| Résolution minimale | 10 m (natif Sentinel-2) | `MIN_RESOLUTION_M` (`api.py`) |
| Analyses par champ | 200 | `MAX_ANALYSES_PER_FIELD` |
| Fréquence de création d'analyses | 60 par heure et par IP | variable `ANALYSIS_RATE_LIMIT` |
| Taille d'une requête | 1 Mo (Content-Length) | variable `MAX_BODY_BYTES` ; à doubler sur le reverse proxy |
| Liste des analyses | 20 par page (max 100) | paramètres `limit` et `offset` |

## Choix de sécurité à noter

- Mots de passe hachés en PBKDF2-HMAC-SHA256 avec sel aléatoire (pas de
  dépendance externe). Pour une vraie mise en production, bcrypt ou argon2
  restent préférables mais PBKDF2 est un choix raisonnable pour un MVP.
- Les erreurs de connexion renvoient volontairement le même message
  ("email ou mot de passe incorrect") que l'email existe ou non, pour éviter
  de révéler quels comptes existent (énumération d'utilisateurs).
- Sessions gérées par token JWT (`tokens.py`), valable 7 jours, transmis via
  l'en-tête `Authorization: Bearer`. Le `user_id` n'est plus jamais transmis
  en clair par le client — il est toujours dérivé du token côté serveur.
- Les réponses d'erreur sont désormais structurées et cohérentes : chaque
  erreur renvoie un objet avec `error`, `detail` et `message`, ce qui facilite
  une intégration côté client et un traitement homogène des validations.
- ⚠️ `JWT_SECRET_KEY` est **obligatoire**. Un secret de développement public
  n'est utilisé que si `ENV=development` (ou `dev`/`local`/`test`) est défini
  explicitement ; sinon l'API refuse de démarrer. Pour le développement local,
  copiez `.env.example` en `.env`. En production, ne définissez pas
  `ENV=development` et fournissez un vrai secret
  (`python -c "import secrets; print(secrets.token_urlsafe(48))"`).
- **Tokens** : signés en HS256 avec PyJWT (`python-jose` a été retiré : faille connue sans
  correctif). `exp` et `sub` sont exigés, et `JWT_SECRET_KEY` doit faire **au moins 32
  caractères** hors `ENV=development`.
- **Mots de passe** : PBKDF2-SHA256, 600 000 itérations. Le coût est stocké avec le hash ; les
  anciens comptes (260 000) sont migrés automatiquement à leur prochaine connexion. Un email
  inconnu dépense le même temps de calcul qu'un compte existant (pas d'énumération par le temps
  de réponse).
- **En-têtes HTTP** : `nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy`, `Permissions-Policy`,
  HSTS hors développement, et une **Content-Security-Policy** sur les pages HTML (`connect-src
  'self'` empêche d'envoyer le token vers un serveur tiers). Pour tester la politique sur de vrais
  appareils sans rien bloquer : `CSP_REPORT_ONLY=1`. Si vous ajoutez une ressource externe
  (police, tuiles, script), ajoutez son hôte à `CONTENT_SECURITY_POLICY` dans `api.py` : un test
  vérifie que toutes les pages restent couvertes.
- **Pas de CDN pour le code** : Leaflet et Leaflet.draw sont hébergés dans `static/vendor/` (voir
  `static/vendor/README.md` pour la provenance et la mise à jour).
- `/docs`, `/redoc` et `/openapi.json` ne sont servis qu'avec `ENV=development`. `/health` ne
  renvoie que `status` et `database` (vérifiée réellement ; 503 si la base ne répond pas).
- Ce que le front affiche venant de l'API passe par `textContent` ou `escapeHtml()`, jamais par
  `innerHTML` brut.
- Le CORS est désormais limité par défaut à des origines locales sûres
  (`localhost` / `127.0.0.1` sur les ports 3000 et 8000), avec possibilité
  de surcharger la liste via `CORS_ALLOWED_ORIGINS`.
- Un utilisateur qui demande les zones d'un champ qui ne lui appartient pas
  reçoit un 404 (pas un 403), pour ne pas révéler l'existence du champ d'un
  autre utilisateur — testé dans `test_api.py`.

## Prochaines étapes, par ordre de priorité

1. **Valider le cœur du flux** sur des parcelles réelles ou des données de
   démo plus proches du terrain, puis ajuster les seuils de zonage si besoin.
2. ✅ **Sécurité d'exploitation renforcée** : `JWT_SECRET_KEY` obligatoire en
   environnement réel, CORS restreint par défaut, secrets gérés via `.env`
   (non versionné), bornes hautes sur les paramètres de zonage (anti-DoS),
   comparaison de mot de passe en temps constant, limites de taille sur les
   champs texte, rate limiting sur `/auth/login` et `/auth/register`.
   Reste à faire : vérification d'email à l'inscription (nécessite un
   service d'envoi externe type SendGrid/Mailgun — reporté volontairement).
3. ✅ **Tests métier ciblés** ajoutés sur la géométrie (bbox/polygone
   invalides, surface minimale, culture inconnue), l'authentification
   (casse/espaces sur l'email, mot de passe trop long, énumération de
   comptes) et les sorties de traitement (un `zone_id` doit désormais
   correspondre à une zone réellement présente dans le résultat de
   l'analyse — c'était un trou de validation avant cette itération).
4. **Préparer une évolution plus robuste si l'usage devient concret** : base de
   données plus solide, logs, monitoring, puis seulement après la stabilité du
   cœur fonctionnel.