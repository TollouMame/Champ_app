// Service worker minimal : met en cache la "coquille" de l'app (HTML, icônes,
// manifest) pour un chargement plus rapide et un affichage possible hors-ligne
// de l'interface — les DONNÉES (zones, champs) nécessitent toujours l'API et
// ne sont pas mises en cache ici, volontairement, pour ne jamais afficher de
// données satellite périmées sans que l'utilisateur le sache.
//
// Un service worker (même minimal) est nécessaire sur Android/Chrome pour que
// le navigateur propose "Installer l'application" — sur iOS/Safari, ce n'est
// pas requis pour "Sur l'écran d'accueil" mais ne gêne pas non plus.

// Incrémenter ce numéro à chaque changement de la coquille pour que les
// appareils déjà installés abandonnent l'ancien cache (cf. 'activate').
const CACHE_NAME = 'champ-app-shell-v3';
const SHELL_FILES = [
  '/onboarding_mobile.html',
  '/apercu_champ_mobile.html',
  '/manifest.json',
  '/icon-192.png',
  '/icon-512.png',
  // Leaflet et Leaflet.draw hébergés localement (cf. static/vendor/README.md)
  '/vendor/leaflet/leaflet.js',
  '/vendor/leaflet/leaflet.css',
  '/vendor/leaflet/images/layers.png',
  '/vendor/leaflet/images/layers-2x.png',
  '/vendor/leaflet/images/marker-icon.png',
  '/vendor/leaflet/images/marker-icon-2x.png',
  '/vendor/leaflet/images/marker-shadow.png',
  '/vendor/leaflet-draw/leaflet.draw.js',
  '/vendor/leaflet-draw/leaflet.draw.css',
  '/vendor/leaflet-draw/images/spritesheet.png',
  '/vendor/leaflet-draw/images/spritesheet-2x.png',
  '/vendor/leaflet-draw/images/spritesheet.svg',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(SHELL_FILES))
  );
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((names) =>
      Promise.all(names.filter((n) => n !== CACHE_NAME).map((n) => caches.delete(n)))
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);

  // ne jamais mettre en cache les appels API (/auth, /fields, /crops) —
  // uniquement la coquille visuelle statique
  if (event.request.method !== 'GET' || !SHELL_FILES.includes(url.pathname)) {
    return;
  }

  const isPage = url.pathname.endsWith('.html') || url.pathname.endsWith('.json');

  if (isPage) {
    // Réseau d'abord : une nouvelle version du HTML/manifest est servie dès
    // qu'on est en ligne ; le cache ne sert que de secours hors-ligne.
    event.respondWith(
      fetch(event.request)
        .then((response) => {
          if (response.ok) {
            const copy = response.clone();
            caches.open(CACHE_NAME).then((cache) => cache.put(event.request, copy));
          }
          return response;
        })
        .catch(() => caches.match(event.request))
    );
  } else {
    // Icônes : cache d'abord (elles changent très rarement).
    event.respondWith(
      caches.match(event.request).then((cached) => cached || fetch(event.request))
    );
  }
});
