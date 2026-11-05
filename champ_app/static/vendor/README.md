# Bibliothèques tierces hébergées localement

Ces fichiers sont servis par l'application elle-même plutôt que depuis un CDN :
un CDN compromis pourrait injecter du code dans la page qui manipule le token de
session, et la carte ne se chargerait pas hors ligne.

| Bibliothèque | Version | Licence | Source | Intégrité du paquet npm (sha512) |
|---|---|---|---|---|
| Leaflet | 1.9.4 | BSD-2-Clause | https://registry.npmjs.org/leaflet/-/leaflet-1.9.4.tgz | `sha512-nxS1ynzJOmOlHp+iL3FyWqK89GtNL8U8rvlMOsQdTTssxZwCXh8N2NB3GDQOL+YR3XnWyZAxwQixURb+FA74PA==` |
| Leaflet.draw | 1.0.4 | MIT | https://registry.npmjs.org/leaflet-draw/-/leaflet-draw-1.0.4.tgz | `sha512-rsQ6saQO5ST5Aj6XRFylr5zvarWgzWnrg46zQ1MEOEIHsppdC/8hnN8qMoFvACsPvTioAuysya/TVtog15tyAQ==` |

Fichiers copiés tels quels depuis `package/dist/` (seule modification : suppression de la ligne
`sourceMappingURL`, pour éviter une requête 404 vers un fichier .map non embarqué).

## Mettre à jour une bibliothèque

1. Télécharger le paquet npm de la nouvelle version et **vérifier son empreinte** (champ `dist.integrity`
   de `https://registry.npmjs.org/<paquet>/<version>`).
2. Remplacer les fichiers du dossier correspondant, mettre à jour ce tableau.
3. Incrémenter `CACHE_NAME` dans `static/service-worker.js` pour que les appareils déjà installés
   récupèrent la nouvelle version.
