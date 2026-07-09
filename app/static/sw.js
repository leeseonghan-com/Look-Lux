// 정산관리 시스템 - 서비스 워커
// 오프라인 시 기본 페이지 캐시 + 정적 자원 캐싱
const CACHE_NAME = 'jeongsan-v1';
const STATIC_ASSETS = [
  '/m/',
  '/m/offline',
  '/static/manifest.json',
  '/static/icon-192.png',
  '/static/icon-512.png',
];

// 설치 시 정적 자원 캐싱
self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(STATIC_ASSETS))
      .then(() => self.skipWaiting())
  );
});

// 이전 캐시 정리
self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

// fetch 전략: 네트워크 우선, 실패 시 캐시 → 오프라인 페이지
self.addEventListener('fetch', (event) => {
  const req = event.request;
  // POST 등은 캐싱 안 함
  if (req.method !== 'GET') return;
  // 외부 URL은 우회
  const url = new URL(req.url);
  if (url.origin !== location.origin) return;

  event.respondWith(
    fetch(req)
      .then((res) => {
        // 정적 자원이면 캐시 갱신
        if (url.pathname.startsWith('/static/')) {
          const resClone = res.clone();
          caches.open(CACHE_NAME).then(c => c.put(req, resClone));
        }
        return res;
      })
      .catch(() => caches.match(req).then(r => r || caches.match('/m/offline')))
  );
});
