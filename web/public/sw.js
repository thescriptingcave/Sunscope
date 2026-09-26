/* Service worker: offline shell only.
 *
 * Deliberately minimal. It caches the app shell so the PWA opens and shows
 * something sensible when the network is gone — and that "something sensible"
 * is the stale-data notice, not fake live numbers.
 *
 * It does NOT cache /api responses. A cached telemetry response that looks
 * fresh is exactly the failure mode a monitoring dashboard must not have, and
 * deciding "is this stale" in the cache is harder than not caching at all.
 * The app already handles the empty-cache case explicitly.
 */

const CACHE = 'solar-shell-v1'
const SHELL = ['/', '/index.html', '/manifest.webmanifest', '/icon.svg']

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE).then((cache) => cache.addAll(SHELL)).then(() => self.skipWaiting()),
  )
})

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  )
})

self.addEventListener('fetch', (event) => {
  const request = event.request
  if (request.method !== 'GET') return

  const url = new URL(request.url)
  if (url.origin !== self.location.origin) return
  // Never cache the API. See the note at the top of this file.
  if (url.pathname.startsWith('/api')) return

  // Network-first for navigations, so a fresh build is picked up promptly.
  if (request.mode === 'navigate') {
    event.respondWith(
      fetch(request).catch(() => caches.match('/index.html')),
    )
    return
  }

  event.respondWith(
    caches.match(request).then((cached) => {
      if (cached) return cached
      return fetch(request).then((response) => {
        if (response.ok && response.type === 'basic') {
          const copy = response.clone()
          caches.open(CACHE).then((cache) => cache.put(request, copy))
        }
        return response
      })
    }),
  )
})
