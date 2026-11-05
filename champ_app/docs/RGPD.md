# Données personnelles et droits des personnes — état technique

> **Ce document décrit ce que le code fait réellement. Ce n'est pas un avis juridique.**
> Les éléments marqués *À faire* (base légale, mentions d'information, durées de conservation,
> sous-traitants) doivent être décidés et validés avec un juriste ou un DPO avant toute mise
> en production auprès de vrais utilisateurs.

## 1. Données traitées

| Donnée | Où | Pourquoi | Remarque |
|---|---|---|---|
| Email, date de création du compte | table `users` | identifier le compte | |
| Mot de passe | table `users` | authentification | jamais stocké en clair : PBKDF2-SHA256 salé, 600 000 itérations |
| Nom, culture, date de semis, contour, surface d'une parcelle | table `fields` | fonction principale | **la localisation d'une parcelle peut identifier son exploitant** : à traiter comme donnée personnelle |
| Analyses (indices NDVI/NDRE, zones, alertes) | table `analyses` | historique | données dérivées de Sentinel-2 pour la parcelle |
| Traitements marqués (zone, note libre, date) | table `zone_treatments` | suivi agronomique | la note est un texte libre saisi par l'utilisateur |
| Journaux applicatifs | sortie standard (JSON) | sécurité, diagnostic | `user_id`, **`email_hash`** (pseudonyme HMAC, jamais l'email), adresse IP du client sur les échecs de connexion, chemins et durées de requête |
| Session dans le navigateur | `localStorage` (`champ_app_session`) | rester connecté | jeton de session, id utilisateur et email |

## 2. Destinataires et services tiers

| Service | Ce qu'il reçoit | Statut |
|---|---|---|
| **Sentinel Hub (Planet)** | emprise (bbox) de la parcelle et date demandée, pas l'identité de l'utilisateur | *À faire* : vérifier les conditions du service et le contrat |
| **OpenStreetMap** (tuiles de carte) | adresse IP et zone affichée | externe par nature ; mentionner dans l'information aux utilisateurs |
| **Google Fonts** | adresse IP de chaque visiteur | *À faire* : héberger les polices localement pour supprimer ce transfert |
| Hébergeur de l'application | toutes les données | *À faire* : choisir, de préférence dans l'UE, et encadrer par contrat |

Leaflet et Leaflet.draw sont servis par l'application elle-même (`static/vendor/`) : aucun transfert vers un CDN.

## 3. Droits des personnes : ce qui est implémenté

| Droit | Implémentation | Interface |
|---|---|---|
| Accès et portabilité | `GET /account/export` : JSON avec compte, parcelles (géométrie comprise), analyses et traitements ; jamais le mot de passe. Limité à 10/heure (`EXPORT_RATE_LIMIT`). | bouton « Télécharger mes données » |
| Effacement | `POST /account/delete` (mot de passe exigé, limite de débit du login) : supprime le compte et tout ce qui s'y rattache. `DELETE /fields/{id}` supprime une seule parcelle. | bouton « Supprimer mon compte et mes données » ; suppression d'une parcelle : API seulement |
| Rectification | **non implémentée** (pas de modification d'un champ, de l'email ou du mot de passe) | *À faire* |
| Limitation, opposition | **non implémentées** (traitement par demande directe) | *À faire* : définir la procédure |

Après une suppression, le jeton de session encore valide n'ouvre plus aucun accès (401), et l'email peut de nouveau servir à créer un compte.

## 4. Ce que l'effacement couvre, et ce qu'il ne couvre pas

**Couvert**
- Les lignes `users`, `fields`, `analyses` et `zone_treatments` du compte.
- Le contenu supprimé est **réellement effacé du fichier SQLite** (`PRAGMA secure_delete`, forcé explicitement : selon la compilation de SQLite, un simple `DELETE` laisse le contenu lisible dans le fichier ; ce cas est testé).

**Non couvert : à traiter**
- **Les sauvegardes** de la base : elles contiennent encore les données supprimées jusqu'à leur expiration. Définir leur durée de vie et la documenter.
- **Les journaux** : ils gardent `user_id`, `email_hash` et des adresses IP. Définir une durée de conservation et la faire respecter côté hébergement (rotation / purge).
- Les fichiers `output/*.json`, `output/*.png` produits par les scripts de démonstration (`demo_flow.py`, `compare_zoning.py`) : sans lien avec des comptes réels.
- Les données déjà transmises à Sentinel Hub, OpenStreetMap ou Google (voir §2).
- Le navigateur de la personne : la suppression de compte efface la session locale ; un export téléchargé reste sur son appareil.

## 5. Reste à faire (hors code)

1. Définir la **base légale** et rédiger la **politique de confidentialité** (finalités, durées, droits, contact, sous-traitants), accessible depuis l'application, avec une information claire à l'inscription.
2. Fixer les **durées de conservation** : comptes inactifs, journaux, sauvegardes ; prévoir la purge automatique correspondante.
3. Tenir un **registre des traitements** (ce document peut servir de base) et vérifier si une analyse d'impact est nécessaire.
4. Préparer la **procédure en cas de violation de données** (notification à l'autorité sous 72 h le cas échéant).
5. Contrats avec les sous-traitants (hébergeur, Sentinel Hub) et choix d'un hébergement dans l'UE.
