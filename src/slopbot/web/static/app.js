'use strict';

const main = document.querySelector('#main');
const state = {me:null, page:'overview', guild:null, feature:null, guilds:[], playlist:'11', days:'7', generation:0, systemTheme:null};
const AUTO_REFRESH_INTERVAL_MS=60*1000;
const AUTO_REFRESH_EDIT_RETRY_MS=30*1000;
const autoRefreshState={timer:null,controller:null};
let installPrompt=null;
const THEME_DEFAULTS={theme_preset:'clear-blue',theme_mode:'system',theme_radius:'balanced',theme_density:'comfortable'};
const THEME_PRESETS={
  'clear-blue':{name:'Clear blue',description:'Crisp neutrals with a familiar blue accent.'},
  'quiet-green':{name:'Quiet green',description:'A calm monochrome base with a restrained green accent.'},
  'soft-indigo':{name:'Soft indigo',description:'Subtle cool surfaces with a softer indigo accent.'},
  'xkcd':{name:'XKCD',description:'Hand-drawn lines, paper-like surfaces, and playful chart energy.'}
};
const names = {
  alerts:'Voice alerts', notifications:'Notification delivery', configurekeywords:'Keyword alerts',
  geton:'Invite friends to play', getonnotifications:'Get-on preferences',
  'rank current':'Current ranks', 'rank peak':'Peak ranks', today:'Server activity',
  matchhistory:'Match history', mmrhistory:'MMR history', movers:'Biggest movers',
  playeroverview:'Player overview', leaderboard:'Rank leaderboard', streakleaderboard:'Streak leaderboard',
  peakleaderboard:'Peak leaderboard', linkrocketleague:'Link Rocket League', unlinkrocketleague:'Unlink Rocket League',
  addepicaccount:'Track an Epic account', removeepicaccount:'Remove an Epic account',
  summarizeserver:'Server configuration', slopbotstatus:'Service status', slopbotstatusalerts:'Personal status alerts',
  slopbotstatuschannel:'Channel status alerts', 'itemshop current':'Current item shop',
  'itemshop changes':'Shop changes', 'itemshop watch':'Create shop watch', 'itemshop watches':'My shop watches',
  'itemshop unwatch':'Remove shop watch', tournaments:'Tournament schedule', tournamentalerts:'Tournament reminders',
  tournamentchannel:'Channel tournament reminders', info:'Slopbot guide',
  'setup events notifications':'Event notifications', 'setup commands visibility':'Discord result visibility',
  'setup commands allow':'Allow a command channel', 'setup commands disallow':'Disallow a command channel',
  'setup commands unrestricted':'Remove channel restrictions', 'setup commands status':'Command policies'
};
function el(tag, attrs={}, ...children) {
  const node=document.createElement(tag);
  for(const [key,value] of Object.entries(attrs)) {
    if(key.startsWith('aria-')&&typeof value==='boolean'){node.setAttribute(key,String(value));continue;}
    if(value===null || value===undefined || value===false) continue;
    if(key==='class') node.className=value;
    else if(key.startsWith('on')) node.addEventListener(key.slice(2),value);
    else if(key==='text') node.textContent=value;
    else node.setAttribute(key,value===true?'':String(value));
  }
  for(const child of children.flat(Infinity)) if(child!==null && child!==undefined) node.append(child instanceof Node?child:document.createTextNode(String(child)));
  return node;
}
function button(text,action,kind=''){return el('button',{class:'btn '+kind,type:'button',onclick:action},text);}
function card(...children){return el('section',{class:'card'},...children);}
function muted(text){return el('p',{class:'muted'},text);}
function heading(title,subtitle,action){return el('div',{class:'page-heading'},el('div',{},el('p',{class:'eyebrow'},state.page==='server'?'CONNECTED WORKSPACE':'ROCKET LEAGUE'),el('h1',{},title),subtitle?muted(subtitle):null),action);}
function empty(title,text){return el('div',{class:'empty'},el('h2',{},title),muted(text));}
function notify(text){const n=document.querySelector('#notice');n.textContent=text;setTimeout(()=>{if(n.textContent===text)n.textContent='';},6000);}
function pwaMode(){return matchMedia('(display-mode: standalone)').matches||navigator.standalone===true;}
function preparePWA(){
  const install=document.querySelector('#install-app');
  if('serviceWorker' in navigator)window.addEventListener('load',()=>{
    const version=document.documentElement.dataset.assetVersion;
    const url=version?`/service-worker.js?v=${encodeURIComponent(version)}`:'/service-worker.js';
    navigator.serviceWorker.register(url,{updateViaCache:'none'}).catch(()=>{});
  });
  window.addEventListener('beforeinstallprompt',event=>{event.preventDefault();installPrompt=event;if(!pwaMode())install.hidden=false;});
  window.addEventListener('appinstalled',()=>{installPrompt=null;install.hidden=true;notify('Slopbot installed.');});
  install.addEventListener('click',async()=>{if(!installPrompt)return;install.disabled=true;try{await installPrompt.prompt();await installPrompt.userChoice;installPrompt=null;install.hidden=true;}finally{install.disabled=false;}});
  if(pwaMode())install.hidden=true;
}
function errorAt(target,error){target.prepend(el('div',{class:'error',role:'alert'},error.message||String(error)));}
function setAutoRefreshStatus(text){document.querySelector('#live-status')?.replaceChildren(document.createTextNode(text));}
function clearAutoRefreshTimer(){if(autoRefreshState.timer!==null){clearTimeout(autoRefreshState.timer);autoRefreshState.timer=null;}}
function stopAutoRefresh(){clearAutoRefreshTimer();autoRefreshState.controller?.abort();autoRefreshState.controller=null;}
function pageLoader(page){return state.me?.user?({overview:overview,sessions:sessionsPage,reports:reportsPage,history:historyPage,'server-activity':serverActivityPage,boards:boardsPage,server:serverPage,admin:adminPage,notifications:notificationsPage,status:statusPage,shop:shopPage,tournaments:tournamentPage,themes:themesPage,account:accountPage})[page]:welcome;}
async function loadPageContent(page){return await pageLoader(page)();}
function hasPendingEdits(){return !!main.querySelector('[data-auto-refresh-dirty],dialog[open],details[open]:not([data-refresh-key]),form:focus-within');}
function replacePreservingDetails(target,...children){
  const key=node=>{
    const path=[];
    for(let current=node;current&&current!==target;current=current.parentElement){
      if(current.dataset.refreshKey)path.unshift(current.dataset.refreshKey);
    }
    return JSON.stringify(path);
  };
  const expanded=new Map();let focused=null;
  const active=document.activeElement;
  const filterFocus=target.contains(active)&&active.matches('[data-refresh-filter]')?
    {key:key(active),start:active.selectionStart,end:active.selectionEnd}:null;
  for(const detail of target.querySelectorAll('details[data-refresh-key]')){
    expanded.set(key(detail),detail.open);
    if(detail.querySelector(':scope > summary')===document.activeElement)focused=key(detail);
  }
  target.replaceChildren(...children);
  for(const detail of target.querySelectorAll('details[data-refresh-key]')){
    const identity=key(detail);
    if(expanded.has(identity))detail.open=expanded.get(identity);
    if(identity===focused)detail.querySelector(':scope > summary')?.focus({preventScroll:true});
  }
  if(filterFocus)for(const filter of target.querySelectorAll('[data-refresh-filter]')){
    if(key(filter)!==filterFocus.key)continue;
    filter.focus({preventScroll:true});
    if(typeof filterFocus.start==='number')filter.setSelectionRange(filterFocus.start,filterFocus.end);
  }
}
function scheduleAutoRefresh(delay=AUTO_REFRESH_INTERVAL_MS,generation=state.generation){
  clearAutoRefreshTimer();
  if(['/privacy','/terms'].includes(location.pathname)||generation!==state.generation)return;
  if(document.visibilityState!=='visible'){setAutoRefreshStatus('Live updates paused · tab hidden');return;}
  autoRefreshState.timer=setTimeout(()=>refreshCurrentPage(generation),delay);
}
async function refreshCurrentPage(generation){
  autoRefreshState.timer=null;
  if(generation!==state.generation||document.visibilityState!=='visible'){
    if(document.visibilityState!=='visible')setAutoRefreshStatus('Live updates paused · tab hidden');
    return;
  }
  if(hasPendingEdits()){
    setAutoRefreshStatus('Live updates paused · finish editing to refresh');
    scheduleAutoRefresh(AUTO_REFRESH_EDIT_RETRY_MS,generation);return;
  }
  const page=state.page,controller=new AbortController();
  autoRefreshState.controller=controller;stopLiveRefresh();
  const refreshGeneration=++state.generation;
  setAutoRefreshStatus('Refreshing live data…');
  let retry=true;
  try{
    const content=await loadPageContent(page);
    if(controller.signal.aborted||refreshGeneration!==state.generation||state.page!==page)return;
    if(hasPendingEdits()){setAutoRefreshStatus('Live updates paused · finish editing to refresh');return;}
    const scrollY=window.scrollY;
    replacePreservingDetails(main,content);window.scrollTo(0,scrollY);
    setAutoRefreshStatus('Updated '+new Date().toLocaleTimeString(undefined,{timeZone:playerTimezone(),hour:'2-digit',minute:'2-digit'}));
  }catch(error){
    if(controller.signal.aborted||refreshGeneration!==state.generation||state.page!==page)return;
    retry=error.status!==401&&error.status!==403;
    setAutoRefreshStatus(retry?'Live update failed · retrying':'Live updates unavailable · access changed');
  }finally{
    if(autoRefreshState.controller===controller)autoRefreshState.controller=null;
    if(retry&&refreshGeneration===state.generation&&state.page===page)scheduleAutoRefresh(AUTO_REFRESH_INTERVAL_MS,refreshGeneration);
  }
}
main.addEventListener('input',event=>{const target=event.target;if(target.matches?.('input,select,textarea')&&!target.matches('[data-refresh-filter]'))(target.closest('form')||target).dataset.autoRefreshDirty='true';});
main.addEventListener('change',event=>{const target=event.target;if(target.matches?.('input,select,textarea')&&!target.matches('[data-refresh-filter]'))(target.closest('form')||target).dataset.autoRefreshDirty='true';});
document.addEventListener('visibilitychange',()=>{
  clearAutoRefreshTimer();
  if(document.visibilityState==='visible'){setAutoRefreshStatus('Live updates active · refreshing within 1 min');scheduleAutoRefresh(0);}
  else setAutoRefreshStatus('Live updates paused · tab hidden');
});
function themeSettings(){return {...THEME_DEFAULTS,...(state.me?.preferences||{})};}
function applyTheme(preferences=themeSettings()){
  const root=document.documentElement,mode=preferences.theme_mode||THEME_DEFAULTS.theme_mode;
  const system=state.systemTheme||(state.systemTheme=matchMedia('(prefers-color-scheme: dark)'));
  root.dataset.themePreset=preferences.theme_preset||THEME_DEFAULTS.theme_preset;
  root.dataset.colorMode=mode==='system'?(system.matches?'dark':'light'):mode;
  root.dataset.themeRadius=preferences.theme_radius||THEME_DEFAULTS.theme_radius;
  root.dataset.themeDensity=preferences.theme_density||THEME_DEFAULTS.theme_density;
  document.querySelector('meta[name="theme-color"]').content=getComputedStyle(root).getPropertyValue('--bg').trim()||(root.dataset.colorMode==='dark'?'#191a1d':'#f7f8fa');
}
function themeChoice(name,value,current,label,description){
  const selected=current===value;
  return el('label',{class:'theme-option'},el('input',{type:'radio',name,value,checked:selected}),el('span',{},el('strong',{},label),el('small',{},description)));
}
function playerTimezone(){return state.me?.preferences?.timezone||state.me?.preferences?.report_view?.timezone||'America/New_York';}
function date(value){return value?new Date(value*1000).toLocaleString(undefined,{timeZone:playerTimezone(),month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}):'—';}
function safeURL(value){try{const u=new URL(value,location.origin);return ['https:','http:'].includes(u.protocol)?u.href:null;}catch{return null;}}
async function api(path,data,signal){
  const options=data===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':state.me?.csrf||''},body:JSON.stringify(data)};
  const requestSignal=signal||(data===undefined?autoRefreshState.controller?.signal:undefined);
  const timeout=!requestSignal&&data===undefined?new AbortController():null;
  const timeoutId=timeout?setTimeout(()=>timeout.abort(),25000):null;
  let response;
  try{response=await fetch(path,{...options,signal:requestSignal||timeout?.signal});}
  catch(error){if(timeout?.signal.aborted)throw Error('This page is taking too long to respond. Please try again.');throw error;}
  finally{if(timeoutId!==null)clearTimeout(timeoutId);}
  const text=await response.text();let result;try{result=JSON.parse(text);}catch{result={error:text};}
  if(!response.ok){const error=new Error(result.error||'Something went wrong. Please try again.');error.status=response.status;throw error;}
  return result;
}
async function loadGuilds(force=false){
  if(!state.me?.user?.discord_id){state.guilds=[];state.guildsFetchedAt=Date.now();return state.guilds;}
  if(!force&&state.guildsFetchedAt&&Date.now()-state.guildsFetchedAt<300000)return state.guilds;
  state.guilds=(await api('/api/guilds')).guilds;state.guildsFetchedAt=Date.now();return state.guilds;
}
function form(fields,submit,label='Save changes'){
  const f=el('form',{class:'form-grid'},fields);const b=el('button',{class:'btn primary',type:'submit'},label);
  f.append(el('div',{class:'wide row'},b));
  f.addEventListener('submit',async event=>{event.preventDefault();f.querySelectorAll('.error').forEach(n=>n.remove());b.disabled=true;try{await submit(f);delete f.dataset.autoRefreshDirty;}catch(error){errorAt(f,error);}finally{b.disabled=false;}});return f;
}
function input(label,name,options={}){return el('label',{},label,el('input',{name,...options}));}
function select(label,name,options,selected='',attrs={}){
  const s=el('select',{name,'aria-label':label,...attrs},options.map(o=>el('option',{value:o.value,selected:String(o.value)===String(selected)},o.label)));
  return el('label',{},label,s);
}
function rich(text){
  const box=el('div',{class:'rich'});
  const raw=String(text||'').replace(/<t:(\d+)(?::[a-zA-Z])?>/g,(_,v)=>date(Number(v))).replace(/<@!?(\d+)>/g,(_,id)=>'@'+(state.guild?.members.find(m=>m.id===id)?.name||'member')).replace(/<#(\d+)>/g,(_,id)=>'#'+(state.guild?.channels.find(c=>c.id===id)?.name||'channel'));
  // Render a small safe Markdown subset using DOM nodes, never HTML from a server.
  const parts=raw.split(/(\*\*[^*]+\*\*|\[[^\]]+\]\(https?:\/\/[^\s)]+\))/g);
  for(const part of parts){
    if(part.startsWith('**')&&part.endsWith('**'))box.append(el('strong',{},part.slice(2,-2)));
    else {const m=part.match(/^\[([^\]]+)\]\(([^)]+)\)$/);if(m&&safeURL(m[2]))box.append(el('a',{href:safeURL(m[2]),target:'_blank',rel:'noopener noreferrer'},m[1]));else box.append(document.createTextNode(part));}
  }return box;
}
function navigation(){
  const items=[['overview','◈','Overview'],['sessions','◷','Sessions'],['history','↗','History'],['reports','▧','Reports'],['server-activity','◉','Server activity'],['boards','≋','Leaderboards'],['server','▤','Server tools'],['shop','◇','Item shop'],['tournaments','⚑','Tournaments'],['notifications','@','Notifications'],['admin','+','Server admin'],['status','o','Status'],['themes','◐','Themes'],['account','◎','Account']];
  document.querySelector('#navigation').replaceChildren(...items.map(([key,symbol,label])=>el('button',{class:'nav-button '+(state.page===key?'active':''),'aria-current':state.page===key?'page':null,type:'button',onclick:()=>navigate(key)},el('span',{class:'nav-symbol','aria-hidden':'true'},symbol),label)));
  const nav=document.querySelector('#navigation'),active=nav.querySelector('.active');
  if(active&&nav.scrollWidth>nav.clientWidth){
    const item=active.getBoundingClientRect(),bounds=nav.getBoundingClientRect();
    if(item.left<bounds.left)nav.scrollLeft+=item.left-bounds.left-8;
    else if(item.right>bounds.right)nav.scrollLeft+=item.right-bounds.right+8;
  }
  document.querySelector('#identity').replaceChildren(...(state.me?.user?[el('span',{class:'avatar'},state.me.user.name.slice(0,1).toUpperCase()),el('span',{},state.me.user.name)]:[el('span',{},'Slopbot')]));
}
async function navigate(page){
  stopAutoRefresh();
  stopLiveRefresh();
  state.page=page;const generation=++state.generation;navigation();document.querySelector('#breadcrumb').textContent=({overview:'Rocket League / Overview',sessions:'Rocket League / Sessions',reports:'Rocket League / Reports',history:'Rocket League / History','server-activity':'Rocket League / Server activity',boards:'Rocket League / Leaderboards',server:'Discord / Server tools',admin:'Discord / Server admin',notifications:'Your notifications',status:'Slopbot / Status',shop:'Rocket League / Item shop',tournaments:'Slopbot / Tournaments',themes:'Slopbot / Themes',account:'Your account'})[page];
  main.replaceChildren(el('div',{class:'loading'},'Loading…'));
  const slowTimer=setTimeout(()=>{if(generation===state.generation)main.replaceChildren(el('div',{class:'loading'},'Still loading… Slopbot is waiting for live data.'));},3000);
  try{
    let content;
    try{content=await loadPageContent(page);}
    catch(error){
      if(![403,404].includes(error.status)||!state.me?.user?.discord_id||!state.guildsFetchedAt)throw error;
      await loadGuilds(true);content=await loadPageContent(page);
    }
    if(generation===state.generation){main.replaceChildren(content);setAutoRefreshStatus('Live updates active · refreshing within 1 min');scheduleAutoRefresh(AUTO_REFRESH_INTERVAL_MS,generation);}
  }catch(error){if(generation===state.generation){main.replaceChildren(heading('Couldn’t load this page'),el('div',{class:'error',role:'alert'},error.message),button('Try again',()=>navigate(page)));scheduleAutoRefresh(AUTO_REFRESH_INTERVAL_MS,generation);}}
  finally{clearTimeout(slowTimer);}
}
function welcome(){return el('div',{class:'stack'},el('section',{class:'card welcome'},el('div',{class:'icon-box','aria-hidden':'true'},'↗'),el('p',{class:'eyebrow'},'SLOPBOT'),el('h1',{},'Rocket League stats and server tools'),muted('Your ranks, your progress, and every result Slopbot observes. Sign in with Google to start tracking Rocket League. Connect Discord whenever you want your server tools, too.'),el('div',{class:'row'},state.me?.google_ready?el('a',{class:'btn primary',href:'/auth/google/start'},'Continue with Google'):el('span',{class:'badge'},'Sign-in is being configured'),el('span',{class:'form-help'},'No Discord account required.'))),el('div',{class:'grid three welcome-grid'},card(el('div',{class:'mini-feature'},el('strong',{},'See your progress'),muted('Current ranks, observed peaks, MMR charts, and streak records.'))),card(el('div',{class:'mini-feature'},el('strong',{},'Follow the game'),muted('Your observed match history, the item shop, and upcoming tournaments.'))),card(el('div',{class:'mini-feature'},el('strong',{},'Bring your server'),muted('Link Discord for alerts, leaderboards, and the settings you can manage.')))));
}
function linkForm(){return form([
  select('Platform','platform',[['epic','Epic Games'],['steam','Steam'],['playstation','PlayStation'],['xbox','Xbox'],['switch','Nintendo Switch']].map(([value,label])=>({value,label})),'epic'),
  input('Raw account ID','account_id',{required:true,maxlength:128,placeholder:'Your platform account ID',pattern:'[A-Za-z0-9:_-]{1,128}'}),
  el('p',{class:'form-help wide'},'Use the platform account ID, not a display name. Tracking verifies that the profile exists; it does not verify ownership. History starts with the first successful sample.')
],async f=>{await api('/api/personal/link',Object.fromEntries(new FormData(f)));notify('Account linked. Tracking will start with the next poll.');await navigate('overview');},'Start tracking');}
function chart(points){
  if(!points.length)return empty('Your history starts here','Slopbot saves MMR changes and hourly continuity points. Come back after your first tracked games.');
  const ns='http://www.w3.org/2000/svg', svg=document.createElementNS(ns,'svg');svg.setAttribute('viewBox','0 0 850 260');svg.setAttribute('class','chart');svg.setAttribute('role','img');svg.setAttribute('aria-label',`MMR history: ${points.length} observations, from ${points[0][1]} to ${points.at(-1)[1]} MMR`);
  const add=(tag,attrs,text)=>{const n=document.createElementNS(ns,tag);for(const[k,v]of Object.entries(attrs))n.setAttribute(k,v);if(text)n.textContent=text;svg.append(n);return n;};
  const values=points.map(p=>p[1]), low=Math.floor(Math.min(...values)/10)*10-10, high=Math.ceil(Math.max(...values)/10)*10+10;
  const start=points[0][0],end=points.at(-1)[0];const x=t=>65+(t-start)/Math.max(1,end-start)*750,y=v=>215-(v-low)/(high-low)*185;
  for(let i=0;i<5;i++){const v=low+(high-low)*i/4,py=y(v);add('line',{x1:60,x2:820,y1:py,y2:py,class:'chart-grid'});add('text',{x:5,y:py+5},String(Math.round(v)));}
  add('polyline',{points:points.map(p=>`${x(p[0])},${y(p[1])}`).join(' '),class:'chart-line'});
  for(const p of [points[0],points.at(-1)])add('circle',{cx:x(p[0]),cy:y(p[1]),r:4,class:'chart-point'});
  add('text',{x:65,y:250},date(start));add('text',{x:815,y:250,'text-anchor':'end'},date(end));return el('div',{class:'chart-scroll'},svg);
}
function table(headers,rows){return el('div',{class:'table-scroll'},el('table',{},el('thead',{},el('tr',{},headers.map(h=>el('th',{scope:'col'},h)))),el('tbody',{},rows.map(row=>el('tr',{},row.map(v=>el('td',{},v??'—')))))));}
function themesPage(){
  const preferences=themeSettings(),preview=el('div',{class:'theme-preview','aria-hidden':'true'},
    el('div',{class:'theme-preview-side'},el('span',{}),el('span',{}),el('span',{})),
    el('div',{class:'theme-preview-main'},el('span',{class:'theme-preview-line'}),el('div',{class:'theme-preview-tiles'},el('span',{}),el('span',{}),el('span',{})),el('span',{class:'theme-preview-chart'})));
  const presets=el('div',{class:'theme-presets'},Object.entries(THEME_PRESETS).map(([key,preset])=>{
    const selected=preferences.theme_preset===key;
    return el('label',{class:'theme-preset'},el('input',{type:'radio',name:'theme_preset',value:key,checked:selected}),
      el('span',{class:'theme-swatches'},[0,1,2].map(index=>el('i',{class:`theme-swatch ${key}-${index}`}))),el('strong',{},preset.name),el('small',{},preset.description));
  }));
  const formNode=form([
    el('fieldset',{class:'theme-fieldset wide'},el('legend',{},'Theme'),presets),
    el('fieldset',{class:'theme-fieldset'},el('legend',{},'Color mode'),el('div',{class:'theme-options'},
      themeChoice('theme_mode','system',preferences.theme_mode,'Use device setting','Match your operating system.'),
      themeChoice('theme_mode','light',preferences.theme_mode,'Light','Always use light mode.'),
      themeChoice('theme_mode','dark',preferences.theme_mode,'Dark','Always use dark mode.'))),
    el('fieldset',{class:'theme-fieldset'},el('legend',{},'Corners'),el('div',{class:'theme-options'},
      themeChoice('theme_radius','square',preferences.theme_radius,'Square','Tighter, more utilitarian corners.'),
      themeChoice('theme_radius','balanced',preferences.theme_radius,'Balanced','The Clear Blue default.'),
      themeChoice('theme_radius','soft',preferences.theme_radius,'Soft','More rounded surfaces.'))),
    el('fieldset',{class:'theme-fieldset'},el('legend',{},'Spacing'),el('div',{class:'theme-options'},
      themeChoice('theme_density','comfortable',preferences.theme_density,'Comfortable','More breathing room.'),
      themeChoice('theme_density','compact',preferences.theme_density,'Compact','Fit more on screen.')))
  ],async f=>{
    const next=Object.fromEntries(new FormData(f));
    applyTheme(next);await api('/api/preferences',next);state.me.preferences={...(state.me.preferences||{}),...next};notify('Theme saved.');
  },'Save theme');
  formNode.addEventListener('change',()=>applyTheme(Object.fromEntries(new FormData(formNode))));
  return el('div',{class:'stack'},heading('Themes','Choose how Slopbot looks on this account.'),preview,card(formNode));
}
async function serverPage(){
  if(!state.me.user.discord_id)return el('div',{class:'stack'},heading('Your Discord servers','Connect your Discord account to bring your Slopbot settings with you.'),card(el('h2',{},'Server tools'),muted('Manage alerts, explore server leaderboards, configure reminders, and access admin controls where you have permission.'),button('Connect Discord',()=>navigate('account'),'primary')));
  state.guilds=await loadGuilds();
  if(!state.guilds.length)return el('div',{class:'stack'},heading('Your Discord servers'),card(empty('No shared servers yet','Your linked Discord account must belong to a server where Slopbot is installed.')));
  const guildId=state.guild?.id&&state.guilds.some(g=>g.id===state.guild.id)?state.guild.id:state.guilds[0].id;
  state.guild=await api('/api/guild/'+guildId);
  const selector=select('Server','guild',state.guilds.map(g=>({value:g.id,label:g.name+(g.admin?' · Admin':'')})),guildId);
  selector.querySelector('select').addEventListener('change',async event=>{state.guild={id:event.target.value};state.feature=null;await navigate('server');});
  const list=el('div',{class:'feature-list',role:'navigation','aria-label':'Server features'}),surface=el('div',{class:'stack'});
  const search=el('input',{type:'search',placeholder:'Find a feature…','aria-label':'Find a server feature'});
  const drawList=()=>{list.replaceChildren();let previous='';for(const c of [...state.guild.commands].sort((a,b)=>a.category.localeCompare(b.category))){const name=names[c.name]||c.description;if(!(name+' '+c.description).toLowerCase().includes(search.value.toLowerCase()))continue;if(c.category!==previous){list.append(el('div',{class:'feature-category'},c.category));previous=c.category;}list.append(el('button',{type:'button',class:'feature-button '+(state.feature===c.name?'active':''),onclick:()=>{state.feature=c.name;drawList();featureForm(surface,c);}},name));}};
  search.addEventListener('input',drawList);state.feature=state.guild.commands.some(c=>c.name===state.feature)?state.feature:state.guild.commands[0]?.name;drawList();
  const selected=state.guild.commands.find(c=>c.name===state.feature);if(selected)featureForm(surface,selected);
  return el('div',{class:'stack'},heading('Server tools','Your existing Slopbot features, connected to the same settings and history.'),selector,el('div',{class:'feature-layout'},el('div',{class:'stack'},search,list),surface));
}
function featureForm(surface,command){
  surface.replaceChildren();
  const fields=[];
  const privateResult=/^(rank |today$|matchhistory$|mmrhistory$|movers$|playeroverview$|leaderboard$|streakleaderboard$|peakleaderboard$|tournaments$|itemshop (current|changes)$)/.test(command.name);
  if(privateResult)fields.push(el('input',{type:'hidden',name:'_context',value:state.guild.channels.find(c=>c.type===0)?.id||state.guild.channels[0]?.id||''}));
  else fields.push(select('Discord context (when needed)','_context',state.guild.channels.map(c=>({value:c.id,label:'#'+c.name})),state.guild.channels.find(c=>c.type===0)?.id||'',{required:true}));
  for(const p of command.parameters){
    let options=p.choices;const label=p.name.replaceAll('_',' ');let field;
    if(p.type==='channel')options=state.guild.channels.filter(c=>!p.channel_types.length||p.channel_types.includes(c.type)).map(c=>({value:c.id,label:'#'+c.name}));
    if(p.type==='user')options=state.guild.members.map(m=>({value:m.id,label:m.name}));
    if(p.name==='account'&&p.autocomplete)options=state.guild.accounts;
    if(p.type==='boolean')options=[{value:'true',label:'Yes'},{value:'false',label:'No'}];
    if(options.length||['channel','user','boolean'].includes(p.type)||p.autocomplete){field=select(label,p.name,[{value:'',label:p.required?'Choose…':'Use default'},...options],'',{required:p.required});}
    else field=input(label,p.name,{type:['integer','number'].includes(p.type)?'number':'text',required:p.required,min:p.type!=='string'?p.min:null,max:p.type!=='string'?p.max:null,minlength:p.type==='string'?p.min:null,maxlength:p.type==='string'?(p.max||1000):null,step:p.type==='integer'?1:'any'});
    field.append(el('span',{class:'form-help'},p.description));fields.push(field);
  }
  const result=el('div',{'aria-live':'polite'});
  const f=form(fields,async formNode=>{const values={};for(const p of command.parameters){let value=formNode.elements.namedItem(p.name).value;if(value==='')continue;if(['integer','number'].includes(p.type))value=Number(value);if(p.type==='boolean')value=value==='true';values[p.name]=value;}
    const flow=await api('/api/command',{command:command.name,guild_id:state.guild.id,channel_id:formNode.elements.namedItem('_context').value,values});renderFlow(result,flow);
  },'Open '+(names[command.name]||'feature').toLowerCase());
  surface.append(card(el('div',{class:'section-top'},el('h2',{},names[command.name]||command.description),command.admin?el('span',{class:'badge'},'Admin'):null),muted(command.description),f,el('p',{class:'note'},'Results appear privately here. The Discord context is used only where a feature needs one; command-channel restrictions apply only in Discord. Invitations and notification settings affect your server.')),result);
}
function renderFlow(target,flow){
  target.replaceChildren();
  for(const message of flow.messages){const box=card(message.content?rich(message.content):null);
    for(const e of message.embeds||[]){const embed=el('div',{class:'embed'},e.title?el('div',{class:'embed-title'},e.title):null,rich(e.description),el('div',{class:'embed-fields'},(e.fields||[]).map(f=>el('div',{class:'embed-field '+(f.inline?'':'full')},el('strong',{},f.name),rich(f.value)))));
      const imageURL=e.image?.url;if(imageURL&&!imageURL.startsWith('attachment:')&&safeURL(imageURL))embed.append(el('img',{class:'result-image',src:safeURL(imageURL),alt:e.title||'Slopbot result',loading:'lazy',referrerpolicy:'no-referrer'}));
      if(e.footer?.text)embed.append(el('p',{class:'note'},e.footer.text));box.append(embed);
    }
    for(const src of message.images||[])if(src.startsWith('data:image/png;base64,'))box.append(el('img',{class:'result-image',src,alt:'Rocket League MMR chart'}));target.append(box);
  }
  const controls=el('div',{class:'controls'});
  const send=async(data,control)=>{if(control)control.disabled=true;try{const next=await api('/api/component',{id:flow.id,revision:flow.revision,...data});renderFlow(target,next);}catch(error){errorAt(target,error);if(control)control.disabled=false;}};
  if(flow.modal){controls.append(card(el('h2',{},flow.modal),form(flow.components.map(c=>input(c.label,String(c.index),{value:c.value,required:c.required,minlength:c.min,maxlength:c.max,placeholder:c.placeholder})),async f=>{await send({values:Object.fromEntries(new FormData(f))});},'Submit')));}
  else for(const c of flow.components){
    if(c.kind==='button'){if(c.url){if(safeURL(c.url))controls.append(el('a',{class:'btn',href:safeURL(c.url),target:'_blank',rel:'noopener noreferrer'},c.label));}else{const b=button(c.label,()=>send({index:c.index},b),c.style==='danger'?'danger':c.style==='success'?'primary':'');b.disabled=c.disabled;controls.append(b);}}
    else if(c.kind==='select'){const s=select(c.label,'selection',c.options,c.max===1?c.options.find(o=>o.default)?.value||'':null,{multiple:c.max>1,disabled:c.disabled});const node=s.querySelector('select');if(c.max>1){for(const option of node.options)option.selected=!!c.options.find(o=>o.value===option.value)?.default;}
      const b=button('Apply selection',()=>send({index:c.index,values:[...node.selectedOptions].map(o=>o.value)},b));b.disabled=c.disabled;s.append(el('span',{class:'form-help'},`Choose ${c.min}–${c.max} option${c.max===1?'':'s'}.`),b);controls.append(s);
    }
  }
  if(flow.components.length)target.append(controls);
}
async function shopPage(){
  const data=await api('/api/shop'),filters={query:'',shop:'all',price:'',sort:'shop'},catalog=el('div',{class:'shop-catalog'}),count=el('span',{class:'badge'});
  const searches=i=>[i.title,i.description,i.shop_name,i.shop_type,...(i.attributes||[]).flat(),...(i.product_ids||[])].join(' ').toLowerCase();
  const prices=i=>(i.prices||[]).map(p=>p.amount),priceText=i=>(i.prices||[]).map(p=>`${Number(p.amount).toLocaleString()} · currency ${p.currency_id}`).join(' / ')||'Price unavailable';
  let guilds=[],guild=null,watchPanel=card(el('h2',{},'Item alerts'),muted('Connect Discord to get notified when matching items change.'),button('Connect Discord',()=>navigate('account'),'primary'));
  const refreshWatches=async id=>{
    guild=guilds.find(g=>g.id===id)||guilds[0];if(!guild)return;
    state.shopGuild=guild.id;
    const settings=await api('/api/settings/'+guild.id),watchList=el('div',{class:'watch-list'}),query=input('Match words','query',{maxlength:100,placeholder:'e.g. crimson octane'}),max=input('Maximum price','max_price',{type:'number',min:0,max:1000000,step:1,placeholder:'Any price'});
    const describe=w=>(w.query?`“${w.query}”`:'All items')+(w.max_price===null?'':` · ${Number(w.max_price).toLocaleString()} or less`);
    const drawWatches=()=>watchList.replaceChildren(...(settings.watches.length?settings.watches.map(w=>el('div',{class:'watch-row'},el('div',{},el('strong',{},describe(w)),el('span',{class:'form-help'},`Watch #${w.id}`)),button('Remove',async()=>{try{await api('/api/settings/'+guild.id+'/personal',{action:'itemshop_unwatch',id:w.id});notify('Shop watch removed.');await refreshWatches(guild.id);}catch(error){errorAt(watchList,error);}},'small danger'))):[empty('No watches yet','Add a broad search or target one item from the catalogue.')]));
    const picker=select('Notification server','watch_guild',guilds.map(g=>({value:g.id,label:g.name})),guild.id);picker.querySelector('select').addEventListener('change',event=>refreshWatches(event.target.value));
    const create=form([query,max,el('p',{class:'form-help wide'},`Matching additions, removals, and updates use this server’s ${settings.delivery} delivery setting. Leave both fields empty to watch every item.`)],async f=>{const raw=f.elements.max_price.value;await api('/api/settings/'+guild.id+'/personal',{action:'itemshop_watch',query:f.elements.query.value,max_price:raw===''?null:Number(raw)});notify('Shop watch saved.');await refreshWatches(guild.id);},'Save watch');
    watchPanel=card(el('div',{class:'section-top'},el('h2',{},'Item alerts'),el('span',{class:'badge'},`${settings.watches.length} / ${settings.item_shop_watch_limit}`)),picker,create,el('h3',{class:'section-space'},'Saved watches'),watchList);drawWatches();
    const old=document.querySelector('#shop-watch-panel');if(old){watchPanel.id='shop-watch-panel';old.replaceWith(watchPanel);}
  };
  if(state.me.user.discord_id){guilds=await loadGuilds();if(guilds.length)await refreshWatches(state.shopGuild&&guilds.some(g=>g.id===state.shopGuild)?state.shopGuild:guilds[0].id);else watchPanel=card(el('h2',{},'Item alerts'),empty('No shared servers','Join a server where Slopbot is installed to create item alerts.'));}
  watchPanel.id='shop-watch-panel';
  const useInWatch=item=>{if(!guild){notify('Connect Discord and choose a server to create a watch.');return;}const field=watchPanel.querySelector('[name="query"]');field.value=item.title;field.focus();watchPanel.scrollIntoView({behavior:'smooth',block:'start'});};
  const draw=()=>{
    const terms=filters.query.trim().toLowerCase().split(/\s+/).filter(Boolean),limit=filters.price===''?null:Number(filters.price);
    let items=data.items.filter(i=>terms.every(term=>searches(i).includes(term))&&(filters.shop==='all'||i.shop_name===filters.shop)&&(limit===null||prices(i).some(p=>p<=limit)));
    items.sort((a,b)=>filters.sort==='price'?(Math.min(...prices(a),Infinity)-Math.min(...prices(b),Infinity)||a.title.localeCompare(b.title)):filters.sort==='ending'?((a.ends_at||Infinity)-(b.ends_at||Infinity)):a.shop_name.localeCompare(b.shop_name)||a.title.localeCompare(b.title));
    count.textContent=`${items.length} of ${data.items.length}`;
    catalog.replaceChildren(...items.map(i=>{const attrs=(i.attributes||[]).map(([key,value])=>el('span',{class:'shop-attribute'},`${key}: ${value}`));return card(i.image_url&&safeURL(i.image_url)?el('img',{class:'shop-image',src:safeURL(i.image_url),alt:i.title||'Shop item',loading:'lazy',referrerpolicy:'no-referrer'}):null,el('div',{class:'shop-card-body'},el('div',{class:'section-top'},el('div',{},el('p',{class:'eyebrow'},i.shop_name||i.shop_type),el('h2',{},i.title||'Shop item')),i.ends_at?el('time',{datetime:new Date(i.ends_at*1000).toISOString()},`Ends ${date(i.ends_at)}`):null),i.description?muted(i.description):null,attrs.length?el('div',{class:'shop-attributes'},attrs):null,el('div',{class:'shop-card-actions'},el('strong',{},priceText(i)),button('Watch this item',()=>useInWatch(i),'small'))));}));
    if(!items.length)catalog.append(empty('No matching items',data.items.length?'Clear a filter or raise the maximum price.':'The catalogue will appear after a successful shop poll.'));
  };
  const search=el('input',{type:'search',placeholder:'Search item, paint, product ID, or shop…','aria-label':'Search item shop'}),shops=[...new Set(data.items.map(i=>i.shop_name).filter(Boolean))].sort(),shop=select('Shop','shop',[{value:'all',label:'All shops'},...shops.map(value=>({value,label:value}))]),price=input('Price at or below','price',{type:'number',min:0,step:1,placeholder:'Any price'}),sort=select('Sort','sort',[{value:'shop',label:'Shop and name'},{value:'price',label:'Lowest price'},{value:'ending',label:'Ending soon'}]);
  search.addEventListener('input',()=>{filters.query=search.value;draw();});shop.querySelector('select').addEventListener('change',event=>{filters.shop=event.target.value;draw();});price.querySelector('input').addEventListener('input',event=>{filters.price=event.target.value;draw();});sort.querySelector('select').addEventListener('change',event=>{filters.sort=event.target.value;draw();});draw();
  const changes=data.changes.slice(0,30),changeFeed=card(el('div',{class:'section-top'},el('h2',{},'Recent changes'),el('span',{class:'badge'},`${changes.length} shown`)),changes.length?el('div',{class:'change-list'},changes.map(c=>el('div',{class:'change-row'},el('span',{class:'change-kind '+c.change_type},c.change_type),el('strong',{},c.item.title),el('span',{class:'muted'},c.item.shop_name||''),el('time',{},date(c.observed_at))))):muted('No shop changes observed yet.'));
  return el('div',{class:'stack'},heading('The item shop','Browse the full saved catalogue, narrow it down quickly, and turn any find into a server-specific alert.',count),el('div',{class:'shop-layout'},el('div',{class:'stack'},card(el('div',{class:'shop-filters'},inputWrap(search),shop,price,sort),catalog),changeFeed),watchPanel),el('p',{class:'note'},'The catalogue refreshes every ten minutes. Prices use the currency IDs supplied by Rocket League; no purchases are made here.'));
}
function inputWrap(node){return el('label',{},'Find items',node);}
async function tournamentPage(){
  const region=state.region||'USE',data=await api('/api/tournaments?region='+region),filter=select('Region','region',data.regions,region);
  filter.addEventListener('change',event=>{state.region=event.target.value;navigate('tournaments');});
  const rows=data.tournaments.filter(t=>t.start_time>Date.now()/1000);
  return el('div',{class:'stack'},heading('Next on the schedule','Upcoming Rocket League tournaments. Times use your saved display timezone.'),filter,card(rows.length?table(['Tournament','Team size','Starts'],rows.slice(0,50).map(t=>[t.title,`${t.team_size}v${t.team_size}`,date(t.start_time)])):empty('No upcoming tournaments','Try another region or check back later.')),card(el('h2',{},'Tournament reminders'),muted('With Discord linked, configure personal reminders and server announcements in Server tools.'),button('Open server tools',()=>navigate('server'))));
}
function legalPage(){const privacy=location.pathname==='/privacy';navigation();main.replaceChildren(el('article',{class:'legal-copy'},el('h1',{},privacy?'Privacy':'Using Slopbot'),...(privacy?[
  ['What we store','When you sign in with Google, Slopbot stores your Google account identifier, name, and email. It stores a Discord user ID only if you link Discord. Session tokens are stored as hashes; OAuth access tokens are used to verify identity and are not retained.'],
  ['Rocket League tracking','Linked profile IDs, names, and observed ranked activity are stored to provide your dashboard. Match and MMR history are retained for 30 days. Observed peak ranks and permanent streak records are retained until the corresponding data is removed. Profile existence checks do not prove ownership.'],
  ['Discord servers','Linking Discord enables access to Slopbot servers you belong to. Server and channel permissions control access. Website actions use the existing Slopbot settings and delivery services. Personal website history is separate from server history.'],
  ['Service providers','Cloudflare carries website traffic. Google handles sign-in; Discord handles account linking and server functionality. Rocket League statistics come through the bot’s configured service. These providers process requests under their own policies.'],
  ['Cookies and deletion','Slopbot uses essential sign-in and session cookies. You can unlink Discord, stop personal tracking, or delete your website account from Account. Website account deletion removes website identity and personal tracking data; existing Discord server data remains with those servers. Contact your Slopbot server administrator about server data.']
]:[
  ['Your account','Use your own Google and Discord accounts. You are responsible for changes and notifications you request through your signed-in session. Only use server tools where you have permission.'],
  ['What the stats mean','Slopbot records observed ranked activity. It cannot backfill matches before tracking begins, and it marks ambiguous polling gaps rather than guessing outcomes. Service interruptions can delay updates.'],
  ['Independent project','Slopbot is an independent project, not an official Rocket League, Epic Games, Google, or Discord product. No purchases or gameplay actions are performed by this website.'],
  ['Availability','Features depend on connected services and may be temporarily unavailable. Do not use the website to send spam or abuse other members. Sign-ups and requests are limited to protect service capacity.']
]).map(([title,text])=>el('section',{},el('h2',{},title),muted(text))),el('a',{class:'btn',href:'/'},'Back to Slopbot')));}
async function boot(page){try{state.me=await api('/api/me');applyTheme();if(!state.systemTheme._slopbotBound){state.systemTheme.addEventListener('change',()=>{if(themeSettings().theme_mode==='system')applyTheme();});state.systemTheme._slopbotBound=true;}if(['/privacy','/terms'].includes(location.pathname)){legalPage();return;}await navigate(page||(location.hash.startsWith('#reports')?'reports':state.page));}catch(error){main.replaceChildren(el('div',{class:'error'},error.message),button('Try again',()=>boot()));}}
preparePWA();
