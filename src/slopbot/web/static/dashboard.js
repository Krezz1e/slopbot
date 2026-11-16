'use strict';

function sessionMMR(value) {
  return value == null ? 'MMR unavailable' : `${signedNumber(value)} MMR`;
}
function sessionMMRValue(value) {
  return el('span',{class:changeClass(value)},sessionMMR(value));
}
function sessionObservationMMR(game) {
  if(game.change==null)return muted('MMR unavailable');
  const value=el('span',{class:changeClass(game.change)},`${signedNumber(game.change)} MMR`);
  return game.total>1?el('span',{},value,` total · ${signedNumber(game.per_game_change)} avg/game`):value;
}
function sessionPlaylistTable(data,session,showOutlook=true) {
  const headings=['Playlist','Wins','Losses','Uncertain','MMR start → end','Change',...(showOutlook?['Current outlook']:[])];
  return el('div',{class:'table-wrap session-playlists'},el('table',{},el('thead',{},el('tr',{},headings.map(t=>el('th',{},t)))),
    el('tbody',{},session.playlists.map(p=>el('tr',{},el('td',{},data.playlists[p.playlist]||p.playlist),el('td',{},p.win),el('td',{},p.loss),el('td',{},p.gap),
      el('td',{},`${p.before==null?'—':Math.round(p.before)} → ${p.after==null?'—':Math.round(p.after)}`),
      el('td',{},p.change==null?'—':el('span',{class:changeClass(p.change)},signedNumber(p.change))),
      ...(showOutlook?[el('td',{},rankOutlook(data.session_rank_estimates?.[p.playlist],true)||'Not enough evidence')]:[]))))));
}
function sessionObservations(data,session) {
  const observations=table(['Observed','Playlist','Results','MMR start → end','Change'],session.observations.map(g=>[
    date(g.at),data.playlists[g.playlist]||g.playlist,
    `${g.wins}W / ${g.losses}L / ${g.unknown}?`,
    g.before==null||g.after==null?'—':`${Math.round(g.before)} → ${Math.round(g.after)}`,sessionObservationMMR(g)]));
  observations.classList.add('session-observations');
  return el('details',{'data-refresh-key':'observations'},el('summary',{},'Game observations'),observations,
    muted('A multi-game row is one polling observation. Its total MMR change is observed; the per-game value is an average because exact individual changes were not available.'));
}
function estimateStepText(step) {
  const result=step.direction==='win'?(step.games===1?'win':'wins'):(step.games===1?'loss':'losses');
  const amount=`~${step.games} ${result}`;
  const target=step.kind==='division'?`Division ${step.target.split('Division ')[1]}`:step.target.split(',')[0];
  return `${amount} to ${step.kind} ${step.direction==='win'?'up':'down'} · ${target}`;
}
function rankOutlook(estimate,compact=false,playlistLabel='') {
  if(!estimate?.steps?.length)return null;
  const groups=['win','loss'].map(direction=>{
    const steps=estimate.steps.filter(step=>step.direction===direction);
    if(!steps.length)return null;
    return el('section',{class:`rank-estimate-group ${direction==='win'?'positive':'negative'}`,
      'aria-label':direction==='win'?'Up estimates':'Down estimates'},
      el('div',{class:'rank-estimate-heading'},direction==='win'?'↗ Up':'↘ Down'),
      ...steps.map(step=>{
        const result=direction==='win'?(step.games===1?'win':'wins'):(step.games===1?'loss':'losses');
        return el('div',{class:'rank-estimate-row','aria-label':estimateStepText(step)},
          el('div',{class:'rank-estimate-label'},el('span',{},step.kind==='division'?'Division':'Rank'),
            el('span',{class:'rank-estimate-target'},step.target.replace(', Division ',' · Div. '))),
          el('div',{class:'rank-estimate-count'},el('strong',{},`~${step.games}`),el('span',{},result)));
      }));
  }).filter(Boolean);
  return el('details',{class:'rank-outlook'+(compact?' compact':''),'aria-label':'Estimated rank outlook','data-refresh-key':`rank:${estimate.playlist}`},
    el('summary',{class:'rank-outlook-toggle'},playlistLabel?`${playlistLabel} · Rank estimates`:'Rank estimates'),
    el('p',{class:'rank-estimate-context'},`${estimate.current_rank}${estimate.observed_at?` · observed ${date(estimate.observed_at)}`:''}`),
    el('div',{class:'rank-estimate-grid'},...groups),
    el('details',{class:'rank-estimate-evidence','data-refresh-key':'evidence'},el('summary',{},'About these estimates'),
      el('p',{},'Assumes consecutive wins or losses. Match difficulty changes MMR gains and losses.'),
      ...estimate.steps.map(step=>el('p',{},`${step.kind==='division'?'Division':'Rank'} ${step.direction==='win'?'up':'down'}: `+
        (step.basis==='usual'?`rough estimate from ${step.boundary_samples} complete rank-step histories.`:
          `estimated boundary ~${step.target_mmr} MMR · ${step.boundary_samples} boundary samples · ${step.delta_samples} ${step.delta_source} result samples.`)))));
}
function sessionWidget(data) {
  const s=data.sessions?.[0];
  const active=s&&!s.closed&&data.session_now<=s.last_at+s.cutoff;
  const subject=data.session_target?`${data.session_target.member_name}'s`:'Your';
  return card(el('div',{class:'section-top'},el('h2',{},active?'Active play session':'Latest play session'),
    button('View sessions',()=>navigate('sessions'),'small')),
    s?el('div',{},el('div',{class:'session-metrics'},datum('Games',s.total),datum('Results',`${s.wins}W · ${s.losses}L`,`${s.unknown} uncertain`),
      datum('Total MMR change',sessionMMRValue(s.mmr_change),'Across playlists'),
      datum(active?'Closes after':'Last played',active?date(s.last_at+s.cutoff):date(s.last_at),`${data.session_scope} · ${s.cutoff/60} min cutoff`)),
      ...s.playlists.map(p=>rankOutlook(data.session_rank_estimates?.[p.playlist],true,data.playlists?.[p.playlist]||String(p.playlist))).filter(Boolean),
      el('details',{class:'session-breakdown','data-refresh-key':'latest-session-breakdown'},el('summary',{},'Playlist breakdown and game observations'),
        sessionPlaylistTable(data,s,false),sessionObservations(data,s))):
      muted(`${subject} next observed game starts a session. All playlists count toward the same session.`));
}
function sessionSettingsFields(p, notifications=true) {
  return [input('Inactivity cutoff (minutes)','cutoff',{type:'number',min:5,max:1440,step:1,required:true,value:p.cutoff/60}),
    toggle('Send session summaries','enabled',notifications&&!!p.enabled),
    input('Minimum games for a summary','minimum',{type:'number',min:1,max:100,step:1,required:true,value:p.minimum}),
    toggle('Include game-by-game observations','include_observations',p.include_observations!==0)];
}
async function sessionsPage() {
  const data=await loadDashboard();
  if(!data.account&&!data.session_target&&!data.session_players?.length)return connectPanel(data);
  const root=el('div',{class:'stack'},heading('Play sessions','See the server’s current or most recent session for every tracked member.',button('Refresh',()=>navigate('sessions'))));
  if(data.account)root.append(profileToolbar(data));
  root.append(sessionMemberToolbar(data),serverSessionOverview(data));
  if(data.session_target)root.append(muted(`Viewing server session history for ${data.session_target.member_name} · ${data.session_target.player_name}.`));
  else if(data.account)root.append(sourceNote(data));
  if(!data.account){
    const range=select('Time range','days',[1,7,14,30].map(d=>({value:d,label:d===1?'Last 24 hours':`Last ${d} days`})),data.days);
    range.addEventListener('change',event=>{state.dashboardFilters.days=Number(event.target.value);navigate('sessions');});
    root.append(range);
    if(!data.session_target){root.append(muted('Choose a member to view their server session history.'));return root;}
  }
  if(data.session_own&&!data.source.personal){
    const scope=select('Session boundaries','scope',[{value:'personal',label:'My personal cutoff'},{value:'server',label:'Server cutoff'}],data.session_scope);
    scope.addEventListener('change',e=>{state.sessionScope=e.target.value;navigate('sessions');});root.append(scope);
  }
  root.append(sessionWidget(data),muted('Times are observed by the tracker, not exact kickoff times. Uncertain results remain marked. The date range filters whole sessions; the playlist filter below finds sessions without splitting them.'));
  if(data.session_own){
    const p=data.session_settings;
    const settings=el('details',{class:'card'},el('summary',{},'Personal session settings'),
      muted('Cutoff changes apply to the next session. Turning summaries off cancels pending notifications. Personal settings apply in this history source; server settings are independent.'),
      form(sessionSettingsFields(p,!data.source.personal),async f=>{
        await api('/api/sessions/settings',{source:data.source.key,cutoff:Number(f.elements.cutoff.value)*60,enabled:f.elements.enabled.checked,minimum:Number(f.elements.minimum.value),include_observations:f.elements.include_observations.checked});
        notify('Session settings saved.');await navigate('sessions');
      }));
    if(data.source.personal){settings.querySelector('[name=enabled]').disabled=true;settings.append(muted('Google-only tracking keeps session history here. To receive Discord or Telegram summaries, link Discord and use a server history source.'));}
    else settings.append(button('Delivery preferences',()=>navigate('notifications'),'small'));
    root.append(settings);
  }
  const views=state.sessionHistoryViews||(state.sessionHistoryViews=new Map());
  const viewKey=JSON.stringify([data.session_target?.key||data.account?.player_id,data.source?.key,data.session_scope]);
  if(!views.has(viewKey))views.set(viewKey,{query:'',playlist:'all',status:'all',page:0});
  const filters=views.get(viewKey);
  const search=el('input',{type:'search',placeholder:'Search session date or playlist…','aria-label':'Search sessions',value:filters.query,'data-refresh-filter':true,'data-refresh-key':'session-search'});
  const playlist=select('Sessions containing playlist','session_playlist',[{value:'all',label:'All playlists'},...Object.entries(data.playlists).map(([value,label])=>({value,label}))],filters.playlist,{'data-refresh-filter':true,'data-refresh-key':'session-playlist'});
  const status=select('Session status','status',[{value:'all',label:'All sessions'},{value:'active',label:'Active'},{value:'complete',label:'Completed'}],filters.status,{'data-refresh-filter':true,'data-refresh-key':'session-status'});
  const list=el('div',{class:'stack'}),pager=el('div',{class:'row','aria-live':'polite'});let page=filters.page;
  const draw=()=>{
    const pid=playlist.querySelector('select').value,kind=status.querySelector('select').value,q=search.value.toLowerCase();
    const rows=(data.sessions||[]).filter(s=>{
      const active=!s.closed&&data.session_now<=s.last_at+s.cutoff;
      return (pid==='all'||s.playlists.some(p=>String(p.playlist)===pid))&&(kind==='all'||(kind==='active')===active)&&
        `${date(s.started_at)} ${s.playlists.map(p=>data.playlists[p.playlist]).join(' ')}`.toLowerCase().includes(q);
    });
    const pages=Math.max(1,Math.ceil(rows.length/10));page=Math.min(page,pages-1);filters.page=page;
    replacePreservingDetails(list,...rows.slice(page*10,page*10+10).map(s=>{
      const active=!s.closed&&data.session_now<=s.last_at+s.cutoff;
      return el('details',{class:'card session-detail','data-refresh-key':`session:${data.session_target?.key||data.account?.player_id}:${data.session_scope}:${s.started_at}`},el('summary',{},el('strong',{},date(s.started_at)),
        el('span',{class:'badge'},active?'Active':'Completed'),el('span',{},`${s.total} games · ${s.wins}W / ${s.losses}L${s.unknown?' / '+s.unknown+'?':''}`),
        el('span',{class:changeClass(s.mmr_change)},sessionMMR(s.mmr_change))),
        el('div',{class:'session-metrics'},datum('Total MMR change',sessionMMRValue(s.mmr_change),'Across playlists'),
          datum('Observed span',Math.round((s.last_at-s.started_at)/60)+' min','First to last observation'),
          datum('Known win rate',s.wins+s.losses?Math.round(s.wins/(s.wins+s.losses)*100)+'%':'—'),
          datum('Best known streaks',`${s.streaks.win}W · ${s.streaks.loss}L`),datum('Cutoff',s.cutoff/60+' min',active?'Waiting for more games':'Session complete')),
        sessionPlaylistTable(data,s),sessionObservations(data,s));
    }));
    if(!rows.length)list.append(empty('No sessions in this view','Try another date range or filter. Session tracking starts with games observed after this feature was enabled.'));
    const prev=button('Previous',()=>{page--;draw();},'small'),next=button('Next',()=>{page++;draw();},'small');prev.disabled=page===0;next.disabled=page===pages-1;
    pager.replaceChildren(prev,el('span',{},`${rows.length} sessions · page ${page+1} of ${pages}`),next);
  };
  for(const control of [search,playlist,status])control.addEventListener(control===search?'input':'change',()=>{
    filters.query=search.value;filters.playlist=playlist.querySelector('select').value;filters.status=status.querySelector('select').value;page=0;draw();
  });
  draw();root.append(el('div',{class:'profile-toolbar'},search,playlist,status),list,pager,muted('Showing up to the 200 most recent sessions in the selected date range. Active sessions update automatically.'));
  return root;
}

function sessionMemberToolbar(data) {
  const options=[{value:'',label:data.account?'My selected profile':'Choose a server member'},...(data.session_players||[]).map(p=>({value:p.key,label:p.label}))];
  const field=select('View a member’s sessions','session_player',options,state.sessionPlayer||'');
  field.querySelector('select').addEventListener('change',event=>{
    state.sessionPlayer=event.target.value||undefined;state.sessionScope=undefined;navigate('sessions');
  });
  return field;
}

function serverSessionOverview(data) {
  const rows=data.session_overview||[];
  const body=rows.length?rows.map(s=>{
    const view=button(s.target_key?'View history':'View my server history',()=>{
      state.sessionPlayer=s.target_key||undefined;state.sessionScope=s.target_key?undefined:'server';navigate('sessions');
    },'small');
    const outlooks=Object.entries(s.rank_estimates||{}).filter(([,estimate])=>estimate.steps?.length);
    return el('div',{class:'session-overview-card','data-refresh-key':`member:${s.target_key}`},
      el('div',{class:'session-overview-row'},el('div',{},el('strong',{},s.member_name),
        el('span',{class:'badge'},s.active?'Current':'Most recent'),el('p',{class:'muted'},`${s.total} games · ${s.wins}W / ${s.losses}L${s.unknown?' / '+s.unknown+'?':''} · ${sessionMMR(s.mmr_change)} · last observed ${date(s.last_at)}`)),view),
      outlooks.length?el('div',{class:'session-preview-estimates'},...outlooks.map(([pid,estimate])=>
        rankOutlook(estimate,true,data.playlists?.[pid]||pid))):
        muted('Not enough rank evidence for estimates yet.'));
  }):[muted('No tracked member has a current or recent server session yet.')];
  return card(el('div',{class:'section-top'},el('h2',{},'Server session overview'),muted('Current sessions take priority; otherwise the latest observed session is shown.')),el('div',{class:'stack'},...body));
}

const HOME_WIDGETS = {
  ranks: 'Current ranks', trend: 'MMR chart', results: 'Recent results',
  streaks: 'Streak records', activity: 'Activity', rewards: 'Season rewards', club: 'Club'
};
const PLAYLIST_SHORT = {10:'1v1',11:'2v2',13:'3v3',27:'Hoops',28:'Rumble',29:'Dropshot',30:'Snow Day'};
const REWARD_NAMES = ['Unranked','Bronze','Silver','Gold','Platinum','Diamond','Champion','Grand Champion','Supersonic Legend'];
state.dashboardFilters = {};
state.boardFilters = {playlist:11,days:7,mode:'current',scope:'main',result:'win'};
state.historyPlayer = undefined;

// Read the coalesced server cache until a cold live request finishes. Keep the
// existing page mounted, and cancel obsolete reads when the user navigates.
const liveRefreshes = new Set();
const liveRefreshResumers = new Set();
function stopLiveRefresh() {
  for(const cancel of liveRefreshes)cancel();
  liveRefreshes.clear();
  liveRefreshResumers.clear();
}
function resumeLiveRefresh() { for(const resume of liveRefreshResumers)resume(); }
function refreshLiveData(url,generation,ready,update,canUpdate=()=>true) {
  if(generation!==state.generation)return;
  const controller=new AbortController();let timer,delay=2000;
  const cancel=()=>{clearTimeout(timer);controller.abort();liveRefreshes.delete(cancel);liveRefreshResumers.delete(resume);};
  const schedule=()=>{timer=setTimeout(poll,delay);};
  const resume=()=>{clearTimeout(timer);timer=setTimeout(poll,0);};
  const poll=async()=>{
    if(generation!==state.generation){cancel();return;}
    if(document.hidden||!canUpdate()){schedule();return;}
    try {
      const data=await api(url,undefined,controller.signal);
      if(controller.signal.aborted||generation!==state.generation)return;
      if(ready(data)){
        if(canUpdate()){update(data);cancel();return;}
      }else if(data.live_available===false||data.account===null){cancel();return;}
    }catch(error){
      if(controller.signal.aborted)return;
      if([401,403,404].includes(error.status)){
        cancel();
        if([401,403].includes(error.status)){
          clearAutoRefreshTimer();
          setAutoRefreshStatus('Live updates unavailable · access changed');
        }
        return;
      }
    }
    delay=Math.min(delay*2,30000);schedule();
  };
  liveRefreshes.add(cancel);liveRefreshResumers.add(resume);schedule();
}

function signedNumber(value) {
  return value == null ? '—' : `${value > 0 ? '+' : ''}${Math.round(value)}`;
}
function changeClass(value) { return value == null || value === 0 ? 'neutral' : value > 0 ? 'positive' : 'negative'; }
function datum(label, value, detail, cls='') {
  return el('div',{class:'datum '+cls},el('span',{class:'datum-label'},label),
    el('strong',{class:'datum-value'},value),detail ? el('span',{class:'datum-detail'},detail) : null);
}
function dashboardQuery() {
  const sessionFilters=state.page==='sessions'?{session_scope:state.sessionScope,session_player:state.sessionPlayer}:{};
  const historyFilters=state.page==='history'?{history_player:state.historyPlayer}:{};
  return new URLSearchParams(Object.entries({...state.dashboardFilters,...sessionFilters,...historyFilters,view:state.page}).filter(([,v])=>v!=='' && v!=null));
}
async function loadDashboard() {
  const data = await api('/api/dashboard?'+dashboardQuery());
  state.guilds=data.guilds||[];state.guildsFetchedAt=Date.now();
  if(data.account&&!(state.page==='history'&&data.history_target)) state.dashboardFilters = {
    player:data.account.player_id, source:state.dashboardFilters.source || '',
    playlist:data.playlist, days:data.days
  };
  return data;
}
async function updateDashboard(patch) {
  state.dashboardFilters = {...state.dashboardFilters,...patch};
  await api('/api/preferences',patch);
  return navigate(state.page);
}
function profileToolbar(data) {
  const profile = data.profiles.find(p=>p.player_id===data.account.player_id);
  const fields = el('div',{class:'profile-toolbar'},
    select('Profile','profile',data.profiles.map(p=>({value:p.player_id,label:p.name})),data.account.player_id),
    select('History source','source',[{value:'',label:'Automatic · longest history'},...profile.sources.map(s=>({value:s.key,label:s.label}))],state.dashboardFilters.source || ''),
    select('Playlist','playlist',Object.entries(data.playlists).map(([value,label])=>({value,label})),data.playlist),
    select('Time range','days',[1,7,14,30].map(d=>({value:d,label:d===1?'Last 24 hours':`Last ${d} days`})),data.days));
  if(state.page==='sessions')fields.querySelector('[name=playlist]').closest('label').remove();
  fields.addEventListener('change',async event=>{
    const {name,value}=event.target;
    if(name==='profile'||name==='source'){state.sessionScope=undefined;state.sessionPlayer=undefined;}
    const patch = name==='profile'?{player:value,source:''}:name==='source'?{source:value}:{[name]:Number(value)};
    try { await updateDashboard(patch); } catch(error) { errorAt(main,error); }
  });
  return fields;
}
function historyToolbar(data) {
  const choices=[{value:'',label:'Me (default)'},...(data.history_players||[]).filter(player=>!player.self).map(player=>({value:player.key,label:player.label}))];
  const fields=[select('Player','history_player',choices,state.historyPlayer||'')];
  if(!data.history_target){
    const profile=data.profiles.find(candidate=>candidate.player_id===data.account.player_id);
    fields.push(
      select('Profile','profile',data.profiles.map(candidate=>({value:candidate.player_id,label:candidate.name})),data.account.player_id),
      select('History source','source',[{value:'',label:'Automatic · longest history'},...profile.sources.map(source=>({value:source.key,label:source.label}))],state.dashboardFilters.source||'')
    );
  }
  fields.push(
    select('Playlist','playlist',Object.entries(data.playlists).map(([value,label])=>({value,label})),data.playlist),
    select('Time range','days',[1,7,14,30].map(days=>({value:days,label:days===1?'Last 24 hours':`Last ${days} days`})),data.days)
  );
  const toolbar=el('div',{class:'profile-toolbar'},fields);
  toolbar.addEventListener('change',async event=>{
    const {name,value}=event.target;
    if(name==='history_player'){
      state.historyPlayer=value||undefined;
      try{await navigate('history');}catch(error){errorAt(main,error);}
      return;
    }
    const patch=name==='profile'?{player:value,source:''}:name==='source'?{source:value}:{[name]:Number(value)};
    try{await updateDashboard(patch);}catch(error){errorAt(main,error);}
  });
  return toolbar;
}
function sourceNote(data) {
  return el('div',{class:'source-note'},el('span',{class:'source-dot'}),
    `History from ${data.source.label}`,
    data.source.first_observed?el('span',{},` · earliest saved observation ${date(data.source.first_observed)}`):null,
    el('span',{class:'source-note-right'},'No records copied or duplicated'));
}
function knownResults(data, playlist=data.playlist) {
  const rows=data.matches.filter(m=>playlist==='all'||m.playlist===Number(playlist));
  const sum=result=>rows.filter(m=>result==='gap'?!['win','loss'].includes(m.result):m.result===result).reduce((n,m)=>n+m.match_count,0);
  const wins=sum('win'),losses=sum('loss'),gaps=sum('gap');
  return {rows,wins,losses,gaps,total:wins+losses,rate:wins+losses?Math.round(wins/(wins+losses)*100):null};
}
function rankTiles(data) {
  const ids=[10,11,13];if(!ids.includes(data.playlist))ids.push(data.playlist);
  return el('div',{class:'rank-tiles'},ids.map(pid=>{
    const live=data.details?.skills.find(s=>s.playlist===pid);
    const sample=data.samples.find(s=>s.playlist===pid);
    const current=live||sample;
    const history=data.histories[pid]||[];
    const delta=history.length>1?history.at(-1)[1]-history[0][1]:null;
    const peak=data.peaks.find(p=>p.playlist===pid);
    return el('section',{class:`rank-tile tone-${pid===10?'blue':pid===11?'violet':pid===13?'teal':'amber'}`},
      el('div',{class:'rank-tile-top'},el('span',{class:'playlist-tag'},PLAYLIST_SHORT[pid]),
        el('span',{class:'delta '+changeClass(delta)},signedNumber(delta)+' MMR')),
      el('div',{class:'rank-tile-main'},el('span',{class:'rank-emblem','aria-hidden':'true'},'◇'),
        el('div',{},el('h2',{},current?.rank||'No rank observed'),
          el('div',{class:'mmr-total'},current?Math.round(current.mmr):'—',el('span',{},' MMR')))),
      rankOutlook(data.rank_estimates?.[pid]),
      el('div',{class:'rank-tile-bottom'},el('span',{},peak?`Peak ${Math.round(peak.mmr)}`:'No observed peak'),
        el('span',{},live?'Current':sample?`Saved · ${date(sample.observed_at)}`:'Awaiting sample')));
  }));
}

function trendChart(series) {
  const usable=series.filter(s=>s.points.length);
  if(!usable.length)return empty('No MMR observations in this range','Change the time range or history source. New profiles start collecting data on the next successful poll.');
  const ns='http://www.w3.org/2000/svg';
  const svg=document.createElementNS(ns,'svg');
  svg.setAttribute('viewBox','0 0 950 310');svg.setAttribute('class','trend-chart');
  svg.setAttribute('role','img');svg.setAttribute('tabindex','0');
  svg.setAttribute('aria-label','MMR history. Use left and right arrow keys to inspect observations.');
  const add=(tag,attrs,text)=>{const n=document.createElementNS(ns,tag);for(const[k,v]of Object.entries(attrs))n.setAttribute(k,v);if(text!==undefined)n.textContent=text;svg.append(n);return n;};
  let minT=Infinity,maxT=-Infinity,low=Infinity,high=-Infinity;
  for(const s of usable)for(const p of s.points){minT=Math.min(minT,p[0]);maxT=Math.max(maxT,p[0]);low=Math.min(low,p[1]);high=Math.max(high,p[1]);}
  const pad=Math.max(10,(high-low)*.18);low=Math.floor(low-pad);high=Math.ceil(high+pad);
  const x=t=>68+(t-minT)/Math.max(1,maxT-minT)*850,y=v=>258-(v-low)/(high-low)*228;
  for(let i=0;i<=4;i++){
    const value=low+(high-low)*i/4,py=y(value);
    add('line',{x1:60,x2:922,y1:py,y2:py,class:'chart-grid'});
    add('text',{x:12,y:py+5},String(Math.round(value)));
  }
  for(const s of usable){
    add('polyline',{points:s.points.map(p=>`${x(p[0])},${y(p[1])}`).join(' '),class:'trend-line '+s.color});
    const last=s.points.at(-1);add('circle',{cx:x(last[0]),cy:y(last[1]),r:4,class:'trend-end '+s.color});
  }
  add('text',{x:68,y:294},date(minT));add('text',{x:918,y:294,'text-anchor':'end'},date(maxT));
  const marker=add('line',{x1:68,x2:68,y1:20,y2:266,class:'chart-cursor'});
  const readout=el('div',{class:'chart-readout','aria-live':'polite'},'Hover over the chart or use the arrow keys to inspect an observation.');
  const timeline=[...new Set(usable.flatMap(s=>s.points.map(p=>p[0])))].sort((a,b)=>a-b);
  let index=timeline.length-1;
  const inspect=t=>{
    marker.setAttribute('x1',x(t));marker.setAttribute('x2',x(t));
    readout.replaceChildren(el('span',{},date(t)),...usable.map(s=>{
      const nearest=s.points.reduce((best,p)=>Math.abs(p[0]-t)<Math.abs(best[0]-t)?p:best);
      return el('strong',{class:s.color},`${s.label}: ${Math.round(nearest[1])} MMR · ${date(nearest[0])}`);
    }));
  };
  svg.addEventListener('pointermove',event=>{
    const rect=svg.getBoundingClientRect(),px=(event.clientX-rect.left)/rect.width*950;
    const t=minT+Math.max(0,Math.min(1,(px-68)/850))*(maxT-minT);
    index=timeline.reduce((best,v,i)=>Math.abs(v-t)<Math.abs(timeline[best]-t)?i:best,0);inspect(timeline[index]);
  });
  svg.addEventListener('keydown',event=>{if(!['ArrowLeft','ArrowRight'].includes(event.key))return;event.preventDefault();index=Math.max(0,Math.min(timeline.length-1,index+(event.key==='ArrowRight'?1:-1)));inspect(timeline[index]);});
  return el('div',{},el('div',{class:'chart-legend'},usable.map(s=>el('span',{class:s.color},'● '+s.label))),
    el('div',{class:'trend-scroll'},svg),readout);
}
function trendWidget(data) {
  const history=data.histories[data.playlist]||[],change=history.length>1?history.at(-1)[1]-history[0][1]:null;
  const compare=el('input',{type:'checkbox',checked:!!data.preferences.compare});
  const graph=el('div');
  const draw=()=>{
    const ids=compare.checked?[...new Set([10,11,13,data.playlist])]:[data.playlist];
    graph.replaceChildren(trendChart(ids.map((id,i)=>({label:PLAYLIST_SHORT[id],points:data.histories[id]||[],color:['blue','violet','teal','amber'][i]}))));
  };
  compare.addEventListener('change',async()=>{draw();try{await api('/api/preferences',{compare:compare.checked});}catch(e){notify(e.message);}});draw();
  return card(el('div',{class:'section-top'},el('div',{},el('h2',{},'MMR history'),
    el('p',{class:'mini-muted'},`${PLAYLIST_SHORT[data.playlist]} · ${data.days===1?'24 hours':data.days+' days'}`)),
    el('span',{class:'large-delta '+changeClass(change)},signedNumber(change)),
    el('label',{class:'check-inline'},compare,'Compare competitive playlists')),
    graph,el('p',{class:'note'},'Observed MMR, not an estimated match timeline. Lines connect recorded samples.'));
}
function resultsWidget(data) {
  const stats=knownResults(data);
  return card(el('div',{class:'section-top'},el('h2',{},'Results'),button('Full history',()=>navigate('history'),'small')),
    el('div',{class:'results-summary'},datum('Wins',stats.wins,'Observed','positive'),datum('Losses',stats.losses,'Observed','negative'),
      datum('Win rate',stats.rate==null?'—':stats.rate+'%','Known results only')),
    stats.rows.length?el('div',{class:'match-strip'},stats.rows.slice(-40).map(m=>el('span',{
      class:'result-chip '+(m.result==='loss'?'loss':m.result==='win'?'':'gap'),
      title:`${date(m.observed_at)} · ${m.match_count} match(es) · ${m.mmr_after==null?'MMR unknown':Math.round(m.mmr_after)+' MMR'}`
    },m.result==='win'?'W':m.result==='loss'?'L':'?'))):muted('No results observed for this playlist yet.'),
    el('p',{class:'note'},`${stats.gaps} uncertain match${stats.gaps===1?'':'es'} excluded from win rate. Newest result on the right.`));
}
function currentStreakGrid(data) {
  return el('div',{class:'record-grid'},['main','all'].map(scope=>{
    const streak=(data.current_streaks||[]).find(item=>item.streak_class===scope);
    const scopeLabel=scope==='main'?'Competitive':'All ranked';
    const result=streak?.result==='win'?(streak.count===1?'win':'wins'):(streak?.count===1?'loss':'losses');
    return datum(scopeLabel,streak?`${streak.count} ${result}`:'—',streak?'Current observed run':'No known current streak',streak?.result==='win'?'positive':streak?.result==='loss'?'negative':'');
  }));
}
function currentStreakWidget(data) {
  return card(el('h2',{},'Current streaks'),currentStreakGrid(data),el('p',{class:'note'},'Competitive combines 1v1, 2v2, and 3v3. All ranked also includes extra modes. Uncertain match intervals reset the known streak.'));
}
function streakWidget(data) {
  return card(el('h2',{},'Streaks'),el('h3',{},'Current'),currentStreakGrid(data),el('h3',{},'All-time records'),el('div',{class:'record-grid'},['main','all'].flatMap(scope=>['win','loss'].map(result=>{
    const record=data.records.find(r=>r.streak_class===scope&&r.result===result);
    return datum(`${scope==='main'?'Competitive':'All ranked'} ${result==='win'?'wins':'losses'}`,record?record.count:'—',record?date(record.observed_at):'No completed record',result==='win'?'positive':'');
  }))),el('p',{class:'note'},'Permanent completed records. Session inactivity does not end an all-time streak.'));
}
function historyEventKind(e) {
  if(['rank_up','rank_down'].includes(e.event_type))return 'rank_change';
  if(['session_streak_completed','streak_completed'].includes(e.event_type))return 'session_streak';
  if(e.event_type==='all_time_streak_completed')return 'all_time_streak';
  if(e.event_type==='streak_leaderboard_movement')return e.scope==='main'?'streak_leaderboard_competitive':'streak_leaderboard_all_ranked';
  return e.event_type;
}
function eventText(e) {
  switch(historyEventKind(e)){
    case 'match': return e.result==='win'?`${e.count} win${e.count===1?'':'s'} observed`:e.result==='loss'?`${e.count} loss${e.count===1?'':'es'} observed`:'Uncertain match interval';
    case 'rank_change': return `${e.rank_before||'Previous rank'} → ${e.rank_after||'New rank'}`;
    case 'peak_mmr': return `New observed peak · ${Math.round(e.mmr_after)} MMR`;
    case 'session_streak': return `${e.count} ${e.result||''} session streak`;
    case 'all_time_streak': return `${e.total_count||e.count} ${e.result||''} all-time streak`;
    case 'leaderboard_movement': return `Leaderboard · #${e.position_before||'—'} → #${e.position_after||'—'}`;
    case 'streak_leaderboard_competitive':
    case 'streak_leaderboard_all_ranked': return `${e.count}-game ${e.result||''} streak entered the ${e.scope==='main'?'competitive':'all-ranked'} board at #${e.position_after}`;
    default:return e.event_type.replaceAll('_',' ');
  }
}
function activityWidget(data, expanded=false) {
  const container=el('div',{class:'activity-list'}),filter=select('Event type','events',[
    {value:'all',label:'All activity'},...['match','rank_change','peak_mmr','session_streak','all_time_streak','leaderboard_movement'].map(value=>({value,label:value.replaceAll('_',' ')}))],'all');
  const draw=()=>{
    const kind=filter.querySelector('select').value;
    const included=state.me.preferences?.history_sections;
    const events=data.events.filter(e=>(!expanded||!included||included.includes(historyEventKind(e)))&&(kind==='all'||historyEventKind(e)===kind)).slice(0,expanded?500:8);
    container.replaceChildren(...events.map(e=>el('div',{class:'event-row'},el('span',{class:'event-dot '+(e.result==='win'?'positive':e.result==='loss'?'negative':'')}),
      el('div',{},el('strong',{},eventText(e)),el('p',{},PLAYLIST_SHORT[e.playlist]||e.scope||'Ranked')),
      el('time',{},date(e.occurred_at)))));
    if(!events.length)container.append(muted('No matching activity in this time range.'));
  };
  filter.addEventListener('change',draw);draw();
  const panel=card(el('div',{class:'section-top'},el('div',{},el('h2',{},'Activity'),muted('All playlists')),filter),container);
  if(expanded){
    const labels={match:'Games and uncertain results',rank_change:'Rank changes',peak_mmr:'New peaks',session_streak:'Session streaks',all_time_streak:'All-time streaks',leaderboard_movement:'Rank leaderboard movements',streak_leaderboard_competitive:'Competitive streak board',streak_leaderboard_all_ranked:'All-ranked streak board'};
    const checks=reportChecks(labels,state.me.preferences?.history_sections||Object.keys(labels),'history_sections');
    panel.prepend(el('details',{},el('summary',{},'Include or omit history event types'),checks));
    checks.addEventListener('change',async()=>{const selected=chosen(checks);state.me.preferences={...(state.me.preferences||{}),history_sections:selected};draw();try{await api('/api/preferences',{history_sections:selected});await navigate('history');}catch(e){notify(e.message);}});
  }
  return panel;
}
function rewardsWidget(data) {
  const rewards=data.details?.rewards;
  return card(el('h2',{},'Season rewards'),rewards?
    el('div',{class:'reward-card'},el('span',{class:'reward-icon','aria-hidden':'true'},'✦'),
      el('div',{},el('strong',{},REWARD_NAMES[rewards.level]||`Level ${rewards.level}`),
        el('p',{class:'mini-muted'},`${rewards.wins} reward wins reported by Rocket League`))):
    muted('Season rewards will appear when live stats are available.'));
}
function clubWidget(data) {
  const club=data.details?.club;
  if(!club)return card(el('h2',{},'Club'),muted(data.details?'This profile has no club.':'Club details are unavailable while live stats are offline.'));
  return card(el('div',{class:'section-top'},el('h2',{},`[${club.tag}] ${club.name}`),club.verified?el('span',{class:'badge'},'Verified'):null),
    muted(club.title||`${club.members.length} members`),
    table(['Member','Role'],club.members.map(m=>[m.player_name,m.player_id===club.owner_player_id?'Owner':`Member · role ${m.role_id}`])),
    club.badges.length?el('div',{class:'club-badges'},club.badges.map(b=>el('span',{class:'badge'},`${b.stat}: ${b.badge}`))):null,
    el('p',{class:'note'},`Club colors: ${club.primary_color} / ${club.accent_color}`));
}
function customization(data, root) {
  const order=[...(data.preferences.widgets||Object.keys(HOME_WIDGETS))];
  const serverCards = defaultServerCards(data).map(c=>({...c}));
  const dialog=el('dialog',{class:'customize-dialog'}),list=el('div',{class:'customize-list'});
  const draw=()=>{
    list.replaceChildren(...[...order,...Object.keys(HOME_WIDGETS).filter(k=>!order.includes(k))].map(key=>{
      const checkbox=el('input',{type:'checkbox',checked:order.includes(key)});
      checkbox.addEventListener('change',()=>{if(checkbox.checked)order.push(key);else order.splice(order.indexOf(key),1);draw();});
      return el('div',{class:'customize-row'},el('label',{class:'check-inline'},checkbox,HOME_WIDGETS[key]));
    }));
  };draw();
  const serverEditor=serverCardEditor(data,serverCards);
  dialog.append(el('h2',{},'Customize overview'),muted('Choose personal sections and add server cards. Use Arrange on the overview to change their order and width. Saved to your account.'),list,serverEditor,
    el('div',{class:'row'},button('Save layout',async()=>{try{await api('/api/preferences',{widgets:order,server_widgets:serverCards});dialog.close();await navigate('overview');}catch(e){errorAt(dialog,e);}},'primary'),button('Cancel',()=>dialog.close())));
  root.append(dialog);dialog.addEventListener('close',()=>dialog.remove());dialog.showModal();
}
function connectPanel(data) {
  const linked=!!state.me.user.discord_id;
  return el('div',{class:'stack'},heading('Rocket League',data.discord_pending?'Discord is reconnecting. Your server profiles will appear when it is ready.':'Connect a profile to see your stats and history.'),
    el('div',{class:'grid two'},card(el('h2',{},linked?'Discord connected':'Already use Slopbot in Discord?'),
      muted(linked?'No Rocket League profile linked to your Discord user was found in shared servers. Add a profile below; if your server already tracks it, Slopbot will reuse it.':'Link Discord to automatically find your existing Rocket League profiles and server history. No Epic ID needed if Slopbot already knows it.'),
      !linked&&state.me.discord_ready?el('a',{class:'btn primary',href:'/auth/discord/start'},'Link Discord'):null),
      card(el('h2',{},'Add a Rocket League profile'),nativeLinkForm(false))),
    card(el('h2',{},'What gets tracked'),muted('Current ranks, observed MMR history, known wins and losses, peaks, streak records, and activity. Gaps stay marked as uncertain. Existing history becomes available when you connect its Discord account.')));
}
async function overview() {
  const generation=state.generation;
  const data=await loadDashboard();
  if(!data.account){
    if(!data.guilds.length)return connectPanel(data);
    const root=el('div',{class:'stack dashboard-root'}),layout=el('div',{class:'dashboard-grid'});
    const arrange=button('Arrange',()=>arrangeDashboard(layout,data,arrange));
    root.append(heading('Overview',state.me.user.name,el('div',{class:'row'},button('Customize',()=>customization(data,root)),arrange)),
      card(el('h2',{},'Your servers are connected'),muted('Server views are ready below. Connect a Rocket League profile to add your personal stats.'),button('Connect a profile',()=>navigate('account'),'small')));
    const servers=await serverWidgets(data);
    [...servers.querySelectorAll('.server-widgets > .card')].forEach((c,index)=>{c.dataset.widgetKey='server-'+index;layout.append(c);});
    applyDashboardLayout(layout,data);root.append(layout);
    return root;
  }
  const root=el('div',{class:'dashboard-root stack'});
  const arrange=button('Arrange',()=>arrangeDashboard(layout,data,arrange));
  root.append(heading(data.account.player_name,'Rocket League overview',el('div',{class:'row'},button('Customize',()=>customization(data,root)),arrange,button('Refresh',()=>navigate('overview'),'primary'))),
    profileToolbar(data),sourceNote(data));
  root.append(sessionWidget(data));
  if(data.service_error)root.append(el('div',{class:'offline-note'},data.service_error));
  const widgets={ranks:rankTiles,trend:trendWidget,results:resultsWidget,streaks:streakWidget,
    activity:activityWidget,rewards:rewardsWidget,club:clubWidget};
  const layout=el('div',{class:'dashboard-grid'});
  for(const key of data.preferences.widgets||Object.keys(HOME_WIDGETS)){
    if(!widgets[key])continue;
    const widget=widgets[key](data);widget.classList.add('widget-'+key);widget.dataset.widgetKey=key;layout.append(widget);
  }
  if(!layout.children.length)layout.append(card(muted('No sections selected.'),button('Choose sections',()=>customization(data,root),'primary')));
  const servers=await serverWidgets(data);
  const serverCards=[...servers.querySelectorAll('.server-widgets > .card')];
  serverCards.forEach((c,index)=>{c.dataset.widgetKey='server-'+index;layout.append(c);});
  applyDashboardLayout(layout,data);
  root.append(layout);
  if(!data.details&&data.live_available!==false)refreshLiveData('/api/dashboard?'+dashboardQuery(),generation,
    fresh=>!!fresh.details,fresh=>{
      Object.assign(data,fresh);
      root.querySelector('.offline-note')?.remove();
      for(const key of ['ranks','rewards','club']){
        const current=layout.querySelector(`[data-widget-key="${key}"]`);
        if(current)replacePreservingDetails(current,...widgets[key](data).childNodes);
      }
    },()=>!layout.classList.contains('arranging')&&!root.querySelector('dialog[open]'));
  return root;
}
async function historyPage() {
  const data=await loadDashboard();if(!data.account)return connectPanel(data);
  const stats=knownResults(data),matchTable=el('div'),filter=select('Results','result',[{value:'all',label:'All results'},{value:'win',label:'Wins'},{value:'loss',label:'Losses'},{value:'gap',label:'Uncertain'}],'all');
  let page=0;const controls=el('div',{class:'row history-pagination'});
  const draw=()=>{
    const outcome=filter.querySelector('select').value;
    const matches=stats.rows.filter(m=>outcome==='all'||(outcome==='gap'?!['win','loss'].includes(m.result):m.result===outcome)).slice().reverse();
    const pages=Math.max(1,Math.ceil(matches.length/25));page=Math.min(page,pages-1);
    const rows=matches.slice(page*25,(page+1)*25);
    matchTable.replaceChildren(rows.length?table(['Observed','Result','Games','MMR','Change'],rows.map(m=>[
      date(m.observed_at),el('span',{class:'result-label '+(m.result==='win'?'positive':m.result==='loss'?'negative':'neutral')},m.result==='win'?'Win':m.result==='loss'?'Loss':'Uncertain'),m.match_count,
      m.mmr_after==null?'—':Math.round(m.mmr_after),el('span',{class:changeClass(m.mmr_after!=null&&m.mmr_before!=null?m.mmr_after-m.mmr_before:null)},signedNumber(m.mmr_after!=null&&m.mmr_before!=null?m.mmr_after-m.mmr_before:null))
    ])):empty('No matching results','Try a different playlist, time range, or result filter.'));
    const previous=button('Previous',()=>{page--;draw();},'small'),next=button('Next',()=>{page++;draw();},'small');
    previous.disabled=page===0;next.disabled=page>=pages-1;
    controls.replaceChildren(previous,el('span',{class:'mini-muted'},`Page ${page+1} of ${pages} · ${matches.length} observations`),next);
  };
  filter.addEventListener('change',()=>{page=0;draw();});draw();
  const exportButton=button('Export CSV',()=>{
    const rows=[['Observed UTC','Playlist','Result','Match count','MMR before','MMR after'],...stats.rows.map(m=>[new Date(m.observed_at*1000).toISOString(),PLAYLIST_SHORT[m.playlist],m.result,m.match_count,m.mmr_before??'',m.mmr_after??''])];
    const blob=new Blob([rows.map(row=>row.map(v=>'"'+String(v).replaceAll('"','""')+'"').join(',')).join('\r\n')],{type:'text/csv'}),url=URL.createObjectURL(blob);
    const a=el('a',{href:url,download:'slopbot-match-history.csv'});a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
  });
  const subtitle=data.history_target?`${data.history_target.member_name} · ${data.source.label}`:data.account.player_name;
  return el('div',{class:'stack'},heading('History',subtitle,exportButton),historyToolbar(data),sourceNote(data),
    currentStreakWidget(data),trendWidget(data),card(el('div',{class:'section-top'},el('h2',{},'Observed matches'),filter),matchTable,controls,
      el('p',{class:'note'},'Unknown intervals can contain multiple games. Observed times are poll times, not exact match completion times.')),
    activityWidget(data,true));
}
async function serverActivityPage() {
  if(!state.me.user.discord_id)return el('div',{class:'stack'},heading('Server activity','Connect Discord to see activity across your servers.'),card(el('h2',{},'Connect Discord'),muted('Server activity is available after your Discord account is linked.'),el('a',{class:'btn primary',href:'/auth/discord/start'},'Link Discord')));
  const guilds=await loadGuilds();
  if(!guilds.length)return el('div',{class:'stack'},heading('Server activity'),card(empty('No shared servers','Join a Discord server with Slopbot to see activity from its tracked members.')));
  const filters=state.serverActivityFilters||(state.serverActivityFilters={guild_id:guilds[0].id,days:7,event:'all'});
  if(!guilds.some(g=>g.id===filters.guild_id))filters.guild_id=guilds[0].id;
  const data=await api('/api/activity?'+new URLSearchParams({guild_id:filters.guild_id,days:filters.days}));
  const toolbar=el('div',{class:'profile-toolbar'},
    select('Server','guild_id',guilds.map(g=>({value:g.id,label:g.name})),filters.guild_id),
    select('Time range','days',[1,7,14,30].map(d=>({value:d,label:d===1?'24 hours':d+' days'})),filters.days),
    select('Event type','event',[{value:'all',label:'All activity'},...['match','rank_change','peak_mmr','session_streak','all_time_streak','leaderboard_movement'].map(value=>({value,label:value.replaceAll('_',' ')}))],filters.event,{'data-refresh-filter':true,'data-refresh-key':'activity-event'}));
  toolbar.addEventListener('change',event=>{
    const name=event.target.name;filters[name]=name==='days'?Number(event.target.value):event.target.value;
    if(name==='event')draw();else navigate('server-activity');
  });
  const feed=el('div',{class:'activity-list'}),draw=()=>{
    const events=data.events.filter(e=>filters.event==='all'||historyEventKind(e)===filters.event);
    feed.replaceChildren(...events.map(e=>el('div',{class:'event-row'},el('span',{class:'event-dot '+(e.result==='win'?'positive':e.result==='loss'?'negative':'')}),
      el('div',{},el('strong',{},e.player_name),el('p',{},eventText(e)+' · '+(PLAYLIST_SHORT[e.playlist]||e.scope||'Ranked'))),el('time',{},date(e.occurred_at)))));
    if(!events.length)feed.append(muted('No matching server activity in this time range.'));
    count.textContent=`${events.length} shown`;
  };
  const count=el('span',{class:'badge','aria-live':'polite'});draw();
  return el('div',{class:'stack'},heading('Server activity',`${data.guild} · all tracked members`,button('Refresh',()=>navigate('server-activity'))),toolbar,
    card(el('div',{class:'section-top'},el('div',{},el('h2',{},'All tracked members'),muted('Activity across your server’s tracked profiles')),count),feed),
    el('p',{class:'note'},'Showing the latest 100 server events in this time range, across all playlists and combined streak records. Only tracked members who are still visible in this server are included.'));
}
async function boardsPage() {
  const generation=state.generation;
  if(!state.me.user.discord_id)return el('div',{class:'stack'},heading('Leaderboards'),card(el('h2',{},'Connect your Discord servers'),muted('Link Discord to see the players your servers already track.'),el('a',{class:'btn primary',href:'/auth/discord/start'},'Link Discord')));
  const guilds=await loadGuilds();
  if(!guilds.length)return el('div',{class:'stack'},heading('Leaderboards'),card(empty('No shared servers','Join a Discord server with Slopbot to see its tracked players.')));
  const filters=state.boardFilters;
  if(!guilds.some(g=>g.id===filters.guild_id))filters.guild_id=guilds[0].id;
  const url='/api/leaderboards?'+new URLSearchParams({guild_id:filters.guild_id,playlist:filters.playlist,days:filters.days});
  const data=await api(url);
  const toolbar=el('div',{class:'profile-toolbar'},
    select('Server','guild_id',guilds.map(g=>({value:g.id,label:g.name})),filters.guild_id),
    select('Playlist','playlist',Object.entries(data.playlists).map(([value,label])=>({value,label})),filters.playlist),
    select('Change over','days',[1,7,14,30].map(d=>({value:d,label:d===1?'24 hours':d+' days'})),filters.days));
  toolbar.addEventListener('change',event=>{filters[event.target.name]=event.target.value;navigate('boards');});
  const tabs=el('div',{class:'segmented',role:'group','aria-label':'Leaderboard type'}),content=el('div',{class:'stack'}),search=el('input',{type:'search',placeholder:'Find a player','aria-label':'Find a leaderboard player',value:filters.query||'','data-refresh-filter':true,'data-refresh-key':'board-search'});
  const draw=(keepControls=false)=>{
    if(keepControls===true&&filters.mode==='streak')return;
    if(keepControls!==true)tabs.replaceChildren(...[['current','Current ranks'],['peak','Peak ranks'],['movers','Movers'],['streak','Streak records']].map(([mode,label])=>el('button',{type:'button',class:filters.mode===mode?'selected':'','aria-pressed':filters.mode===mode,onclick:()=>{filters.mode=mode;draw();}},label)));
    const query=search.value.toLowerCase();let rows=[...data.rows];
    if(filters.mode==='streak'){
      const scope=select('Scope','scope',[{value:'main',label:'Competitive'},{value:'all',label:'All ranked'}],filters.scope,{'data-refresh-filter':true,'data-refresh-key':'board-scope'});
      const result=select('Record','result',[{value:'win',label:'Win streaks'},{value:'loss',label:'Loss streaks'}],filters.result,{'data-refresh-filter':true,'data-refresh-key':'board-result'});
      for(const control of [scope,result])control.addEventListener('change',event=>{filters[event.target.name]=event.target.value;draw();});
      const records=(data.records[filters.scope+':'+filters.result]||[]).map((r,i)=>({...r,position:i+1})).filter(r=>r.player_name.toLowerCase().includes(query));
      content.replaceChildren(el('div',{class:'row'},scope,result),card(records.length?table(['#','Player','Games','Completed'],records.map(r=>[r.position,r.player_name,r.count,date(r.observed_at)])):empty(query?'No matching players':'No completed streak records',query?'Try another name or clear the search.':'Records appear when a tracked permanent streak ends.')));return;
    }
    const mode=filters.mode;
    if(mode==='peak')rows.sort((a,b)=>(b.peak?.mmr??-1)-(a.peak?.mmr??-1));
    if(mode==='movers')rows=rows.filter(r=>r.change!=null).sort((a,b)=>b.change-a.change);
    rows=rows.map((r,i)=>({...r,position:i+1})).filter(r=>r.name.toLowerCase().includes(query));
    const top=rows.slice(0,3);
    const podium = el('div',{class:'podium'},top.map(r=>{
      const metric=mode==='peak'?r.peak:r.skill;
      return el('section',{class:'card podium-card'},
        el('span',{class:'podium-place'},String(r.position).padStart(2,'0')),
        el('div',{},el('h2',{},r.name,r.you?el('span',{class:'you-tag'},'you'):null),
          el('strong',{class:'podium-value'},mode==='movers'?signedNumber(r.change):metric?Math.round(metric.mmr):'—')),
        el('p',{class:'mini-muted'},mode==='movers'?`MMR over ${filters.days} days`:metric?.rank||'Not observed'));
    }));
    const standings = rows.length ? table(['#','Player',mode==='peak'?'Peak MMR':'MMR','Rank',`${filters.days}d change`],rows.map(r=>{
      const metric=mode==='peak'?r.peak:r.skill;
      return [r.position,el('strong',{},r.name,r.you?el('span',{class:'you-tag'},'you'):null),
        metric?Math.round(metric.mmr):'—',metric?.rank||'Not observed',
        el('span',{class:changeClass(r.change)},signedNumber(r.change))];
    })) : empty(query?'No matching players':'No tracked results',query?'Try another name or clear the search.':'Players and results will appear as Slopbot observes them.');
    content.replaceChildren(podium,card(standings));
  };
  search.addEventListener('input',()=>{filters.query=search.value;draw();});draw();
  const root=el('div',{class:'stack'},heading('Leaderboards',data.guild),toolbar,
    !data.live&&data.rows.length?el('div',{class:'offline-note'},data.live_available===false?'Showing saved ranks. Live stats are currently unavailable.':'Showing saved ranks. Live stats refresh automatically when available.'):null,
    tabs,search,content,el('p',{class:'note'},'These private web views use your server membership. Discord command-channel restrictions do not apply here.'));
  if(!data.live&&data.rows.length&&data.live_available!==false)refreshLiveData(url,generation,fresh=>fresh.live,fresh=>{
    Object.assign(data,fresh);root.querySelector('.offline-note')?.remove();draw(true);
  });
  return root;
}
function nativeLinkForm(hasPersonal) {
  const fields=[select('Platform','platform',[['epic','Epic Games'],['steam','Steam'],['playstation','PlayStation'],['xbox','Xbox'],['switch','Nintendo Switch']].map(([value,label])=>({value,label})),'epic'),
    input('Raw account ID','account_id',{required:true,maxlength:128,pattern:'[A-Za-z0-9:_-]{1,128}',placeholder:'Platform account ID'})];
  if(hasPersonal)fields.push(el('label',{class:'check-inline wide'},el('input',{type:'checkbox',name:'replace'}),'Replace my personal profile if this is a different account'));
  fields.push(el('p',{class:'form-help wide'},'Existing accessible profiles are reused. A new personal profile is created only if this account is not already known. Profile existence does not verify ownership.'));
  return form(fields,async f=>{
    const values=Object.fromEntries(new FormData(f));if(hasPersonal)values.replace=!!f.elements.namedItem('replace').checked;
    const result=await api('/api/personal/link',values);
    state.dashboardFilters={};notify(result.reused?'Existing profile connected. No duplicate created.':'Profile connected. Tracking starts on the next successful poll.');await navigate('overview');
  },'Connect profile');
}
async function accountPage() {
  const data=await api('/api/dashboard?view=account'),u=state.me.user;
  const personal=data.profiles.some(p=>p.sources.some(s=>s.personal));
  const root=el('div',{class:'stack'},heading('Account','Connected identities and Rocket League profiles'),
    el('div',{class:'identity-grid'},card(el('span',{class:'identity-label'},'GOOGLE'),el('h2',{},u.name),muted(u.email),button('Sign out',async()=>{await api('/api/logout',{});state.dashboardFilters={};await boot();})),
      card(el('span',{class:'identity-label'},'DISCORD'),el('h2',{},u.discord_id?'Connected':'Not linked'),muted(u.discord_id?'Your profiles and server settings are available automatically.':'Link Discord to discover existing profiles, history, preferences, and server tools.'),
        u.discord_id?button('Unlink Discord',async()=>{if(!confirm('Unlink Discord? Server histories will no longer be accessible here. Your personal tracking and Discord server data will remain.'))return;await api('/api/discord/unlink',{});state.dashboardFilters={};await boot('account');},'danger'):
          state.me.discord_ready?el('a',{class:'btn primary',href:'/auth/discord/start'},'Link Discord'):muted('Discord linking is not configured.'))),
    card(el('h2',{},'Connected Rocket League profiles'),data.profiles.length?el('div',{class:'connected-profiles'},data.profiles.map(p=>el('div',{class:'connected-profile'},el('div',{},el('strong',{},p.name),el('p',{class:'mini-muted'},p.sources.map(s=>s.label).join(' · '))),button('View stats',async()=>{state.dashboardFilters={player:p.player_id,source:''};await api('/api/preferences',{player:p.player_id,source:''});await navigate('overview');},'small')))):muted('No profiles connected yet.'),
      el('p',{class:'note'},'Matching accounts are shown once. Histories stay with their original sources; you can select a source on Overview. Discord settings remain available in Server tools.')));
  const add=el('details',{class:'card'},el('summary',{},data.profiles.length?'Connect another profile':'Connect a Rocket League profile'),el('div',{class:'section-space'},nativeLinkForm(personal)));
  root.append(card(el('h2',{},'Display timezone'),
    form([input('Preferred timezone (IANA)','timezone',{value:playerTimezone(),required:true,maxlength:100})],async f=>{
      const timezone=f.elements.timezone.value.trim();await api('/api/preferences',{timezone});
      state.me.preferences={...(state.me.preferences||{}),timezone,report_view:{...(state.me.preferences?.report_view||{}),timezone}};
      if(state.reportFilters)state.reportFilters.timezone=timezone;
      notify('Timezone saved.');await navigate('account');
    },'Save timezone'),muted('Used for displayed dates and new report timezone fields. Existing delivery schedules keep their saved timezone.')));
  if(!data.profiles.length)add.open=true;
  root.append(add);
  if(personal)root.append(card(el('h2',{},'Personal tracking'),muted('Stop tracking the profile you added directly to this website. This does not remove any Discord server links.'),button('Stop personal tracking',async()=>{if(!confirm('Stop personal tracking? Discord tracking will continue unchanged.'))return;await api('/api/personal/unlink',{});state.dashboardFilters={};await navigate('account');},'danger')));
  root.append(el('details',{class:'card'},el('summary',{},'Delete website account'),muted('Deletes your website identity, preferences, and personal tracking. Discord server data remains.'),
    form([input('Type DELETE to confirm','confirm',{required:true,pattern:'DELETE'})],async f=>{await api('/api/account/delete',Object.fromEntries(new FormData(f)));state.dashboardFilters={};await boot();},'Delete my website account')));
  return root;
}
window.addEventListener('unhandledrejection',event=>{notify(event.reason?.message||'Something went wrong. Please try again.');});

function defaultServerCards(data) {
  if(data.preferences.server_widgets)return data.preferences.server_widgets.map(config=>({...config,kind:config.kind==='activity'?'server_activity':config.kind}));
  if(!data.guilds.length)return [];
  return ['ranks','server_activity'].map(kind=>({kind,guild_id:data.guilds[0].id,playlist:11,days:7}));
}

function applyDashboardLayout(layout,data) {
  const order=data.preferences.layout||[];
  const original=[...layout.children];
  const positions=new Map(original.map((node,index)=>[node,index]));
  original.sort((a,b)=>{
    const ia=order.indexOf(a.dataset.widgetKey),ib=order.indexOf(b.dataset.widgetKey);
    return (ia<0?100+positions.get(a):ia)-(ib<0?100+positions.get(b):ib);
  }).forEach(node=>layout.append(node));
  for(const node of layout.children){
    if(!node.dataset.widgetKey)continue;
    node.classList.add('dashboard-widget');
    const size=data.preferences.card_sizes?.[node.dataset.widgetKey]||(['ranks','trend','activity','club'].includes(node.dataset.widgetKey)?2:1);
    node.dataset.size=size;node.classList.add(size===2?'size-wide':'size-half');
  }
}
async function arrangeDashboard(layout,data,toggle) {
  if(layout.classList.contains('arranging')){
    const nodes=[...layout.querySelectorAll(':scope > [data-widget-key]')];
    toggle.disabled=true;
    try{
      await api('/api/preferences',{layout:nodes.map(n=>n.dataset.widgetKey),card_sizes:Object.fromEntries(nodes.map(n=>[n.dataset.widgetKey,Number(n.dataset.size)]))});
      layout.classList.remove('arranging');layout.querySelectorAll('.widget-editbar').forEach(n=>n.remove());
      nodes.forEach(n=>{n.draggable=false;n.ondragstart=null;n.ondragover=null;n.ondrop=null;n.ondragend=null;});
      toggle.textContent='Arrange';resumeLiveRefresh();notify('Layout saved.');
    }catch(error){notify(error.message);}finally{toggle.disabled=false;}return;
  }
  layout.classList.add('arranging');toggle.textContent='Save arrangement';let dragged=null;
  for(const node of layout.children){
    if(!node.dataset.widgetKey)continue;
    node.draggable=true;
    node.ondragstart=event=>{dragged=node;event.dataTransfer.effectAllowed='move';event.dataTransfer.setData('text/plain',node.dataset.widgetKey);node.classList.add('dragging');};
    node.ondragend=()=>{node.classList.remove('dragging');dragged=null;};
    node.ondragover=event=>{if(dragged&&dragged!==node){event.preventDefault();event.dataTransfer.dropEffect='move';}};
    node.ondrop=event=>{event.preventDefault();if(dragged&&dragged!==node)layout.insertBefore(dragged,node);};
    const size=button(Number(node.dataset.size)===2?'Half width':'Full width',()=>{
      const wide=Number(node.dataset.size)!==2;node.dataset.size=wide?2:1;
      node.classList.toggle('size-wide',wide);node.classList.toggle('size-half',!wide);size.textContent=wide?'Half width':'Full width';
    },'small');
    const label=HOME_WIDGETS[node.dataset.widgetKey]||node.querySelector('h2')?.textContent||'Server card';
    const earlier=button('←',()=>{if(node.previousElementSibling)layout.insertBefore(node,node.previousElementSibling);},'small');earlier.setAttribute('aria-label','Move '+label+' earlier');
    const later=button('→',()=>{if(node.nextElementSibling)layout.insertBefore(node.nextElementSibling,node);},'small');later.setAttribute('aria-label','Move '+label+' later');
    node.prepend(el('div',{class:'widget-editbar'},el('span',{},'⠿ '+label),earlier,later,size));
  }
  notify('Drag cards to reorder, or use the arrow and width buttons. Save when finished.');
}
function serverCardEditor(data,cards) {
  const root=el('section',{class:'server-card-editor'}),list=el('div');
  const draw=()=>{
    list.replaceChildren(...cards.map((config,index)=>{
      const fields=el('div',{class:'server-card-fields'},
        select('View','kind',[['ranks','Ranks'],['server_activity','Server activity'],['movers','Movers'],['streaks','Streak records']].map(([value,label])=>({value,label})),config.kind),
        select('Server','guild_id',data.guilds.map(g=>({value:g.id,label:g.name})),config.guild_id),
        ['activity','server_activity'].includes(config.kind)?null:select('Playlist','playlist',Object.entries(data.playlists).map(([value,label])=>({value,label})),config.playlist),
        select('Time range','days',[1,7,14,30].map(d=>({value:d,label:d+' days'})),config.days));
      fields.addEventListener('change',event=>{const {name,value}=event.target;config[name]=['playlist','days'].includes(name)?Number(value):value;if(name==='kind')draw();});
      return el('div',{class:'server-card-config'},fields,el('div',{class:'row'},
        button('Move up',()=>{if(index){[cards[index-1],cards[index]]=[cards[index],cards[index-1]];draw();}},'small'),
        button('Remove',()=>{cards.splice(index,1);draw();},'small danger')));
    }));
  };draw();
  root.append(el('h3',{},'Server cards'),list);
  if(data.guilds.length)root.append(button('Add server card',()=>{if(cards.length>=8){notify('You can add up to eight server cards.');return;}cards.push({kind:'ranks',guild_id:data.guilds[0].id,playlist:11,days:7});draw();},'small'));
  else root.append(muted('Link Discord to configure server cards.'));
  return root;
}
async function serverWidgets(data) {
  const configs=defaultServerCards(data);
  if(!configs.length)return el('div');
  const root=el('div',{class:'stack'},el('div',{class:'section-top'},el('h2',{},'Your servers'))),grid=el('div',{class:'server-widgets'}),requests=new Map();
  const cards=await Promise.all(configs.map(async config=>{
    const guild=data.guilds.find(g=>g.id===config.guild_id);
    if(!guild)return card(el('h3',{},'Server unavailable'),muted('You no longer share this server with Slopbot. Remove this card in Customize.'));
    const activity=['activity','server_activity'].includes(config.kind);
    const key=(activity?'/api/activity?':'/api/leaderboards?')+new URLSearchParams({guild_id:config.guild_id,days:config.days,...(activity?{}:{playlist:config.playlist})});
    try{
      if(!requests.has(key))requests.set(key,api(key));
      const board=await requests.get(key),titles={ranks:'Ranks',activity:'Server activity',server_activity:'Server activity',movers:'Movers',streaks:'Streak records'};
      const box=card(el('div',{class:'section-top'},el('div',{},el('p',{class:'server-card-label'},guild.name),el('h2',{},titles[config.kind])),
        el('span',{class:'badge'},activity?'All playlists':config.kind==='streaks'?'Competitive':PLAYLIST_SHORT[config.playlist])),
        el('div',{class:'server-card-meta'},`${activity?'All tracked members':board.rows.length+' tracked profiles'} · ${config.days} day${config.days===1?'':'s'}`));
      if(activity){
        const events=board.events.slice(0,6);
        box.append(events.length?el('div',{class:'activity-list'},events.map(e=>el('div',{class:'event-row'},el('span',{class:'event-dot'}),el('div',{},el('strong',{},e.player_name),el('p',{},eventText(e))),el('time',{},date(e.occurred_at))))):muted('No activity in this range.'));
      }else if(config.kind==='streaks'){
        const rows=(board.records['main:win']||[]).slice(0,5);
        box.append(rows.length?table(['Player','Wins','Completed'],rows.map(r=>[r.player_name,r.count,date(r.observed_at)])):muted('No completed competitive win streaks yet.'));
      }else{
        let rows=board.rows;
        if(config.kind==='movers')rows=rows.filter(r=>r.change!=null).sort((a,b)=>Math.abs(b.change)-Math.abs(a.change));
        box.append(rows.length?table(['Player','MMR','Change'],rows.slice(0,5).map(r=>[el('strong',{},r.name,r.you?el('span',{class:'you-tag'},'you'):null),r.skill?Math.round(r.skill.mmr):'—',el('span',{class:changeClass(r.change)},signedNumber(r.change))])):muted('No observations yet.'));
      }
      box.append(button(activity?'Open server activity':'Open leaderboard',()=>{
        if(activity){state.serverActivityFilters={guild_id:config.guild_id,days:config.days,event:'all'};navigate('server-activity');}
        else{state.boardFilters={...state.boardFilters,...config,mode:config.kind==='streaks'?'streak':config.kind==='movers'?'movers':'current'};navigate('boards');}
      },'small'));
      return box;
    }catch(error){return card(el('h3',{},guild.name),muted(error.message));}
  }));
  grid.append(...cards);root.append(grid);return root;
}
boot();
