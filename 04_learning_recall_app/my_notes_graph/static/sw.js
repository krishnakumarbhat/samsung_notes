/* Recall service worker — offline shell for the study workstation.
 *
 * Three caches with different lifetimes:
 *   shell  the app itself (HTML/CSS/JS), refreshed in the background
 *   vendor local KaTeX/Tailwind/highlight bundles and their fonts (immutable)
 *   api    last successful GET /api/quiz/* and /api/sync/pull_deck payloads,
 *          only used when the network is unavailable
 *
 * POST requests (review uploads) are never cached: the page queues them in
 * IndexedDB and retries, because a failed write must remain visible to the app.
 */
'use strict';

const VERSION = 'recall-v5';
const SHELL_CACHE = `${VERSION}-shell`;
const VENDOR_CACHE = `${VERSION}-vendor`;
const API_CACHE = `${VERSION}-api`;

const SHELL = [
  '/',
  '/static/index.html',
  '/static/workstation.css',
  '/static/workstation.js',
  '/static/flow.js',
  '/static/games.js',
  '/static/manifest.webmanifest',
  '/static/icon-192.png',
  '/static/icon-512.png'
];

const VENDOR = [
  '/static/vendor/tailwind.min.css',
  '/static/vendor/highlight.min.css',
  '/static/vendor/highlight.min.js',
  '/static/vendor/katex.min.css',
  '/static/vendor/katex.min.js',
  '/static/vendor/auto-render.min.js'
];

const CACHEABLE_API = /^\/api\/(elo|quiz\/(stats|queue)|sync\/(pull_deck|pull_batch|export_deck)|games\/(topics|scores|sudoku|match|trap_hunt))/;

self.addEventListener('install', event => {
  event.waitUntil((async () => {
    const shell = await caches.open(SHELL_CACHE);
    const vendor = await caches.open(VENDOR_CACHE);
    // One missing asset must not block the whole install.
    await Promise.allSettled([
      ...SHELL.map(url => shell.add(new Request(url, { cache: 'reload' }))),
      ...VENDOR.map(url => vendor.add(new Request(url, { cache: 'reload' })))
    ]);
    await self.skipWaiting();
  })());
});

self.addEventListener('activate', event => {
  event.waitUntil((async () => {
    const keep = new Set([SHELL_CACHE, VENDOR_CACHE, API_CACHE]);
    for (const key of await caches.keys()) {
      if (!keep.has(key)) await caches.delete(key);
    }
    await self.clients.claim();
  })());
});

async function networkFirst(request, cacheName) {
  const cache = await caches.open(cacheName);
  try {
    const response = await fetch(request);
    if (response && response.ok) cache.put(request, response.clone());
    return response;
  } catch (error) {
    const cached = await cache.match(request);
    if (cached) return cached;
    throw error;
  }
}

async function staleWhileRevalidate(request, cacheName) {
  const cache = await caches.open(cacheName);
  const cached = await cache.match(request);
  const network = fetch(request).then(response => {
    if (response && response.ok) cache.put(request, response.clone());
    return response;
  }).catch(() => null);
  return cached || (await network) || Response.error();
}

self.addEventListener('fetch', event => {
  const request = event.request;
  if (request.method !== 'GET') return;              // uploads handled by the page
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  if (request.mode === 'navigate') {
    event.respondWith((async () => {
      try {
        const response = await fetch(request);
        const cache = await caches.open(SHELL_CACHE);
        cache.put('/', response.clone());
        return response;
      } catch (error) {
        const cache = await caches.open(SHELL_CACHE);
        return (await cache.match('/')) || (await cache.match('/static/index.html')) || Response.error();
      }
    })());
    return;
  }

  if (url.pathname.startsWith('/api/')) {
    event.respondWith(CACHEABLE_API.test(url.pathname)
      ? networkFirst(request, API_CACHE)
      : fetch(request));
    return;
  }

  if (url.pathname.startsWith('/static/vendor/')) {
    event.respondWith(staleWhileRevalidate(request, VENDOR_CACHE));
    return;
  }

  if (url.pathname === '/manifest.webmanifest' || url.pathname.startsWith('/static/')) {
    event.respondWith(staleWhileRevalidate(request, SHELL_CACHE));
  }
});
