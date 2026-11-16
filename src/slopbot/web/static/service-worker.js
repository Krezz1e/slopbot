'use strict';

const VERSION='__SLOPBOT_ASSET_VERSION__';
const CACHE=`slopbot-shell-${VERSION}`;
const versioned=path=>`${path}${path.includes('?')?'&':'?'}v=${VERSION}`;
const SHELL=['/offline',...[
  '/manifest.webmanifest',
  '/static/app.css',
  '/static/dashboard.css',
  '/static/theme.css',
  '/static/refresh.css',
  '/static/comic-neue-regular.woff2',
  '/static/comic-neue-bold.woff2',
  '/static/app.js',
  '/static/controls.js',
  '/static/dashboard.js',
  '/static/reports.js',
  '/static/icon-192.png',
  '/static/icon-512.png'
].map(versioned)];

self.addEventListener('install',event=>{
  event.waitUntil(caches.open(CACHE).then(cache=>cache.addAll(SHELL)).then(()=>self.skipWaiting()));
});

self.addEventListener('activate',event=>{
  event.waitUntil(caches.keys().then(keys=>Promise.all(keys.filter(key=>key!==CACHE).map(key=>caches.delete(key)))).then(()=>self.clients.claim()));
});

self.addEventListener('fetch',event=>{
  const request=event.request,url=new URL(request.url);
  if(request.method!=='GET'||url.origin!==self.location.origin||url.pathname.startsWith('/api/')||url.pathname.startsWith('/auth/'))return;
  if(request.mode==='navigate'){
    event.respondWith(fetch(request).catch(()=>caches.match('/offline')));
    return;
  }
  if(url.pathname==='/manifest.webmanifest'||url.pathname.startsWith('/static/')){
    const fallback=()=>caches.match(request).then(cached=>cached||caches.match(versioned(url.pathname)));
    if(url.searchParams.get('v')===VERSION){
      event.respondWith(caches.match(request).then(cached=>cached||fetch(request)));
      return;
    }
    event.respondWith(fetch(request).then(response=>{
      if(!response.ok)return response;
      const copy=response.clone();
      return caches.open(CACHE).then(cache=>cache.put(request,copy)).then(()=>response);
    }).catch(fallback));
  }
});

self.addEventListener('push',event=>{
  let data={title:'Slopbot notification',body:'Open Slopbot for details.',url:'/'};
  try{data={...data,...event.data.json()};}catch{}
  event.waitUntil(self.registration.showNotification(data.title,{body:data.body,icon:data.icon||'/static/icon-192.png',badge:data.badge||'/static/icon-192.png',data:{url:data.url||'/'}}));
});

self.addEventListener('notificationclick',event=>{
  event.notification.close();
  const target=new URL(event.notification.data?.url||'/',self.location.origin).href;
  event.waitUntil(self.clients.matchAll({type:'window',includeUncontrolled:true}).then(clients=>{
    const client=clients.find(item=>item.url.startsWith(self.location.origin));
    if(client)return client.navigate(target).then(()=>client.focus());
    return self.clients.openWindow(target);
  }));
});
