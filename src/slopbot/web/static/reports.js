'use strict';

function reportChecks(labels, selected, name) {
  return el('div',{class:'report-checks'},Object.entries(labels).map(([value,label])=>
    el('label',{class:'check-inline'},el('input',{type:'checkbox',name,value,checked:selected.map(String).includes(value)}),label)));
}
function reportDate(stamp, zone) {
  return new Intl.DateTimeFormat(undefined,{timeZone:zone,day:'numeric',month:'short',year:'numeric'}).format(new Date(stamp*1000));
}
function reportTimestamp(stamp, zone) {
  return new Intl.DateTimeFormat(undefined,{timeZone:zone,day:'numeric',month:'short',year:'numeric',hour:'numeric',minute:'2-digit',timeZoneName:'short'}).format(new Date(stamp*1000));
}
function reportMMR(value) { return value==null?'Unavailable':`${value>=0?'+':''}${Math.round(value)}`; }
function reportSections(data,p,prefix) {
  const ordered=[...p.sections,...Object.keys(data.sections).filter(k=>!p.sections.includes(k))];
  const sections=reportChecks(Object.fromEntries(ordered.map(k=>[k,data.sections[k]])),p.sections,prefix+'_sections');
  for(const label of [...sections.children]){
    const name=label.querySelector('input').value;
    const move=direction=>{const sibling=direction<0?label.previousElementSibling:label.nextElementSibling;if(sibling)sections.insertBefore(direction<0?label:sibling,direction<0?sibling:label);};
    const controls=el('span',{class:'report-order'},button('↑',()=>move(-1)),button('↓',()=>move(1)));
    controls.children[0].setAttribute('aria-label','Move '+data.sections[name]+' up');controls.children[1].setAttribute('aria-label','Move '+data.sections[name]+' down');
    label.append(controls);
  }
  const preset=select('Content preset',prefix+'_preset',[{value:'',label:'Custom'},
    {value:'all',label:'Everything'},{value:'progress',label:'Progress'},{value:'highlights',label:'Highlights only'},{value:'results',label:'Results only'},
    ...Object.keys(state.me.preferences?.report_presets||{}).map(k=>({value:'saved:'+k,label:k}))],'');
  preset.addEventListener('change',()=>{
    const value=preset.querySelector('select').value, builtins={all:Object.keys(data.sections),progress:['results','mmr','comparison','activity','peak_mmr','rank_change'],highlights:Object.keys(data.sections).filter(k=>!['results','mmr','players','playlists','comparison','activity'].includes(k)),results:['results','players','playlists','activity']};
    const keys=value.startsWith('saved:')?state.me.preferences.report_presets[value.slice(6)]:builtins[value];
    if(!keys)return;for(const key of [...keys,...Object.keys(data.sections).filter(k=>!keys.includes(k))]){const box=[...sections.querySelectorAll('input')].find(b=>b.value===key);box.checked=keys.includes(key);sections.append(box.closest('label'));}
  });
  const presetName=el('input',{type:'text',maxlength:40,placeholder:'Preset name','aria-label':'Preset name'});
  const savePreset=button('Save content preset',async()=>{try{
    const name=presetName.value.trim(),keys=chosen(sections);if(!name||!keys.length)throw Error('Name your preset and select at least one section.');
    const presets={...(state.me.preferences?.report_presets||{}),[name]:keys};await api('/api/preferences',{report_presets:presets});state.me.preferences.report_presets=presets;notify('Content preset saved.');
    const menu=preset.querySelector('select');if(![...menu.options].some(o=>o.value==='saved:'+name))menu.append(el('option',{value:'saved:'+name},name));menu.value='saved:'+name;
  }catch(e){notify(e.message);}});
  const deletePreset=button('Delete selected preset',async()=>{try{const menu=preset.querySelector('select'),key=menu.value;if(!key.startsWith('saved:'))throw Error('Select one of your saved presets.');const presets={...state.me.preferences.report_presets};delete presets[key.slice(6)];await api('/api/preferences',{report_presets:presets});state.me.preferences.report_presets=presets;menu.selectedOptions[0].remove();menu.value='';notify('Preset deleted.');}catch(e){notify(e.message);}});
  const playlists=reportChecks(data.playlists,p.playlists.length?p.playlists:Object.keys(data.playlists),prefix+'_playlists');
  return {sections,playlists,node:el('div',{class:'stack wide'},
    el('div',{class:'profile-toolbar'},preset,presetName,savePreset,deletePreset),
    el('fieldset',{class:'theme-fieldset'},el('legend',{},'Include in this report'),sections),
    el('fieldset',{class:'theme-fieldset'},el('legend',{},'Playlists'),playlists),
    muted('Use arrows to order sections. Combined streaks span playlists and remain included when selected. Clear a section to omit it.'))};
}
function reportDetailControls(p){return [input('Minimum streak length','minimum_streak',{type:'number',min:1,max:1000,value:p.minimum_streak,required:true}),
  input('Minimum new peak gain (MMR)','minimum_peak_gain',{type:'number',min:0,max:1000,value:p.minimum_peak_gain,required:true}),
  select('Default player order','player_order',[{value:'activity',label:'Most games'},{value:'gains',label:'Largest MMR gains'},{value:'losses',label:'Largest MMR losses'},{value:'name',label:'Name'}],p.player_order)];}
function reportDetails(f){return {minimum_streak:Number(f.elements.minimum_streak.value),minimum_peak_gain:Number(f.elements.minimum_peak_gain.value),player_order:f.elements.player_order.value};}
function selectedReportPlaylists(checks,data) {
  const values=chosen(checks).map(Number);
  if(!values.length)throw Error('Include at least one playlist.');
  return values.length===Object.keys(data.playlists).length?[]:values;
}
function reportExports(data,link){
  const r=data.report;
  const download=(content,extension,type)=>{const url=URL.createObjectURL(new Blob([content],{type}));el('a',{href:url,download:`slopbot-${r.cadence}-${r.start}.${extension}`}).click();setTimeout(()=>URL.revokeObjectURL(url),1000);};
  return el('div',{class:'profile-toolbar report-exports'},
    button('Download text',()=>download(data.full_text,'txt','text/plain;charset=utf-8')),
    button('Download CSV',()=>download('\uFEFF'+data.csv,'csv','text/csv;charset=utf-8')),
    button('Print / Save PDF',()=>{
      document.querySelector('#report-print')?.remove();const print=el('article',{id:'report-print'},el('h1',{},`${data.name} · ${r.cadence} report`));
      const charts=reportCharts(r);if(charts)print.append(charts);
      print.append(el('pre',{},data.full_text));document.body.append(print);document.body.classList.add('printing-report');
      window.addEventListener('afterprint',()=>{print.remove();document.body.classList.remove('printing-report');},{once:true});window.print();
    }),
    button('Copy report link',async()=>{try{const query=new URLSearchParams(Object.entries(link).map(([k,v])=>[k,Array.isArray(v)?v.join(','):String(v)]));await navigator.clipboard.writeText(location.origin+'/#reports?'+query);notify('Report link copied. Recipients must sign in and have access.');}catch(e){notify(e.message);}}));
}
function reportChart(title,labels,series){
  const svg=(tag,attributes={},text)=>{const node=document.createElementNS('http://www.w3.org/2000/svg',tag);for(const [k,v] of Object.entries(attributes))node.setAttribute(k,String(v));if(text!=null)node.textContent=text;return node;};
  const canvas=svg('svg',{viewBox:'0 0 720 260',role:'img','aria-label':title,class:'report-chart'});canvas.append(svg('title',{},title));
  const values=series.flatMap(s=>s.values).filter(v=>v!=null),low=Math.min(0,...values),high=Math.max(1,...values),y=v=>210-(v-low)/(high-low)*185;
  canvas.append(svg('line',{x1:48,x2:704,y1:y(0),y2:y(0),class:'report-axis'}));
  for(const v of [low,high])canvas.append(svg('text',{x:42,y:y(v)+4,'text-anchor':'end',class:'report-tick'},Math.round(v)));
  const width=640/Math.max(1,labels.length),bar=Math.min(48,width/(series.length+1));
  labels.forEach((label,i)=>{
    canvas.append(svg('text',{x:60+width*(i+.5),y:235,'text-anchor':'middle',class:'report-tick'},label.length>13?label.slice(0,12)+'…':label));
    series.forEach((s,j)=>{const value=s.values[i];if(value==null)return;const rect=svg('rect',{x:60+width*(i+.5)+(j-series.length/2)*bar,y:Math.min(y(value),y(0)),width:Math.max(1,bar-2),height:Math.max(1,Math.abs(y(value)-y(0))),class:'report-bar '+s.color});rect.append(svg('title',{},`${label} · ${s.name}: ${value}`));canvas.append(rect);});
  });
  if(!values.length)canvas.append(svg('text',{x:360,y:125,'text-anchor':'middle',class:'report-tick'},'No measured values'));
  return card(el('h3',{},title),canvas,el('div',{class:'report-legend'},series.map(s=>el('span',{class:s.color},s.name))));
}
function reportCharts(r){
  const charts=[],s=r.sections;
  if(s.includes('activity')&&r.daily?.length){
    const labels=r.daily.map(d=>d.date.slice(5));
    charts.push(reportChart('Games by day',labels,s.includes('results')?[
      {name:'Wins',color:'chart-win',values:r.daily.map(d=>d.wins)},{name:'Losses',color:'chart-loss',values:r.daily.map(d=>d.losses)},{name:'Uncertain',color:'chart-unknown',values:r.daily.map(d=>d.unknown)}]:[{name:'Games',color:'chart-win',values:r.daily.map(d=>d.games)}]));
    if(s.includes('mmr'))charts.push(reportChart('Net MMR by day',labels,[{name:'Net MMR',color:'chart-mmr',values:r.daily.map(d=>d.mmr)}]));
  }
  if(s.includes('players')&&s.includes('mmr')&&r.players.length){const players=[...r.players].filter(p=>p.mmr!=null).sort((a,b)=>Math.abs(b.mmr)-Math.abs(a.mmr)).slice(0,8);
    if(players.length)charts.push(reportChart('Largest player MMR changes',players.map(p=>p.name),[{name:'Net MMR',color:'chart-mmr',values:players.map(p=>p.mmr)}]));}
  return charts.length?el('div',{class:'grid two report-charts'},charts):null;
}
async function reportsPage() {
  const guilds=state.me.user.discord_id?await loadGuilds():[];
  const saved=state.me.preferences?.report_view||{};
  const filters=state.reportFilters||(state.reportFilters={...saved});
  if(!state.reportLinkRead){state.reportLinkRead=true;const link=new URLSearchParams(location.hash.split('?')[1]||'');
    for(const key of ['guild_id','cadence','scope','timezone','weekday','sections','playlists','minimum_streak','minimum_peak_gain','player_order','range_start','range_end','players'])if(link.has(key))filters[key]=['sections','playlists','players'].includes(key)?link.get(key).split(',').filter(Boolean).map(v=>key==='playlists'?Number(v):v):['weekday','minimum_streak','minimum_peak_gain'].includes(key)?Number(link.get(key)):link.get(key);
    state.reportLinkedDate=link.get('date');state.reportSnapshot=link.has('delivery')?{guild_id:link.get('guild_id'),owner:link.get('owner'),delivery:link.get('delivery')}:null;
  }
  const personal=String(-Number(state.me.user.id));
  const sources=[...guilds.map(g=>({value:g.id,label:g.name})),{value:personal,label:'Personal tracking'}];
  if(!sources.some(s=>s.value===filters.guild_id)){filters.guild_id=guilds[0]?.id||personal;delete filters.players;}
  filters.cadence=filters.cadence||'daily';filters.scope=filters.scope||'self';filters.offset=filters.offset??1;
  if(filters.guild_id===personal)filters.scope='self';
  const query=new URLSearchParams(Object.entries(filters).filter(([,v])=>v!=null).map(([k,v])=>[k,Array.isArray(v)?v.join(','):String(v)]));
  if(state.reportLinkedDate)query.set('date',state.reportLinkedDate);
  const data=await api('/api/reports?'+query),r=data.report;
  filters.offset=data.offset;state.reportLinkedDate=null;
  if(state.reportSnapshot){const link=state.reportSnapshot;
    try{const snapshot=await api('/api/reports/snapshot?'+new URLSearchParams(link));return el('div',{class:'stack'},heading('Delivered report snapshot','Content as generated for this delivery.',button('Browse reports',()=>{state.reportSnapshot=null;return navigate('reports');})),reportExports(snapshot,link),reportBody(snapshot));}catch(e){state.reportSnapshot=null;notify(e.message);}}
  const persist=async()=>{const savedView=Object.fromEntries(Object.entries(filters).filter(([k])=>!['range_start','range_end','players'].includes(k)));await api('/api/preferences',{report_view:savedView});state.me.preferences={...(state.me.preferences||{}),report_view:savedView,...(savedView.timezone?{timezone:savedView.timezone}:{})};};
  const change=async(key,value)=>{
    if(key==='offset'){
      if(value==='custom'){filters.range_start=data.date_limits.min;filters.range_end=data.date_limits.max;value=0;}
      else {delete filters.range_start;delete filters.range_end;}
    }
    filters[key]=value;
    if(['guild_id','scope','cadence'].includes(key)){
      delete filters.sections;delete filters.playlists;delete filters.weekday;filters.offset=1;
    }
    if(['guild_id','scope'].includes(key))delete filters.players;
    if(key==='cadence'){delete filters.range_start;delete filters.range_end;}
    if(filters.guild_id===personal)filters.scope='self';
    await persist();await navigate('reports');
  };
  const toolbar=el('div',{class:'profile-toolbar'},
    select('History source','guild_id',sources,filters.guild_id),
    select('Report','cadence',[{value:'daily',label:'Daily'},{value:'weekly',label:'Weekly'}],filters.cadence),
    select('Activity','scope',[{value:'self',label:'Just me'},...(filters.guild_id===personal?[]:[{value:'server',label:'Whole server'}])],filters.scope),
    select('Period','offset',[{value:'custom',label:'Custom date range'},...data.periods.map(p=>({value:String(p.offset),label:p.offset===0?'Current period · in progress':`${reportDate(p.start,r.timezone)} – ${reportDate(p.end-1,r.timezone)}`}))],filters.range_start?'custom':String(filters.offset)));
  toolbar.addEventListener('change',e=>change(e.target.name,e.target.name==='offset'&&e.target.value!=='custom'?Number(e.target.value):e.target.value).catch(e=>notify(e.message)));
  const datePicker=filters.range_start?form([
    input('From date','range_start',{type:'date',required:true,min:data.date_limits.min,max:data.date_limits.max,value:filters.range_start}),
    input('To date','range_end',{type:'date',required:true,min:data.date_limits.min,max:data.date_limits.max,value:filters.range_end})
  ],async f=>{
    const first=f.elements.range_start.value,last=f.elements.range_end.value;
    if(first>last)throw Error('From date must be on or before To date.');
    filters.range_start=first;filters.range_end=last;await navigate('reports');
  },'View date range'):null;
  const playerChecks=el('div',{class:'channel-checks'},(data.available_players||[]).map(p=>
    el('label',{class:'check-inline'},el('input',{type:'checkbox',value:p.id,checked:data.selected_players.includes(p.id)}),p.name)));
  const playerPicker=filters.scope==='server'?el('details',{class:'card'},
    el('summary',{},`Included players (${data.selected_players.length} of ${data.available_players.length})`),
    form([el('div',{class:'row'},button('Select everyone',()=>playerChecks.querySelectorAll('input').forEach(n=>n.checked=true),'small'),
      button('Clear selection',()=>playerChecks.querySelectorAll('input').forEach(n=>n.checked=false),'small')),playerChecks],async()=>{
        const selected=chosen(playerChecks);
        if(!selected.length)throw Error('Include at least one player.');
        filters.players=selected;await navigate('reports');
      },'Apply player selection')):null;
  const picks=reportSections(data,data.config,'view');
  const customize=el('details',{class:'card'},el('summary',{},'Customize report content and calendar'),
    form([input('Report timezone (IANA)','timezone',{value:r.timezone,required:true,maxlength:100}),
      select('Week starts','weekday',['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday'].map((label,value)=>({value:String(value),label})),String(data.config.weekday)),...reportDetailControls(data.config),picks.node],async f=>{
        filters.timezone=f.elements.timezone.value;filters.weekday=Number(f.elements.weekday.value);
        filters.sections=chosen(picks.sections);filters.playlists=selectedReportPlaylists(picks.playlists,data);
        Object.assign(filters,reportDetails(f));
        if(!filters.sections.length)throw Error('Include at least one section.');
        await persist();await navigate('reports');
      },'Update report view'),muted('Daily and weekly reports are automatically available for retained dates. Your report view timezone and content choices are saved to your account; no scheduling is required.'));
  const output=reportBody(data);
  const exportButton=reportExports(data,{...filters,...Object.fromEntries(['timezone','weekday','sections','playlists','minimum_streak','minimum_peak_gain','player_order'].map(k=>[k,data.config[k]])),...(filters.range_start?{}:{date:r.start})});
  const root=el('div',{class:'stack'},heading('Reports','Rocket League activity for calendar periods or a custom date range.',exportButton),toolbar,datePicker,playerPicker,
    el('div',{class:'section-top'},muted(`${reportDate(r.start,r.timezone)} – ${reportDate(r.scheduled_end-1,r.timezone)} · ${r.timezone}`),
      el('span',{class:'badge'},r.complete?'Completed period':'In progress')),customize,output);
  const alerts=el('details',{class:'card',id:'report-alerts'},el('summary',{},'Personal daily and weekly alerts'),
    muted('Each cadence and scope is independent. Personal alerts use this server’s Discord/Telegram delivery preference. Saving starts with the next delivery; disabling cancels pending reports.'));
  if(filters.guild_id===personal)alerts.append(muted('Personal tracking supports report browsing. Link Discord and choose a shared server to receive alerts.'));
  else alerts.append(el('div',{class:'grid two'},data.personal_settings.map(s=>reportSchedule(data,s,'personal'))),reportDeliveryHistory(data.deliveries));
  root.append(alerts);
  if(data.admin){const schedules=el('div',{class:'grid two'},data.server_settings.map(s=>reportSchedule(data,s,'channel')));
    const add=cadence=>{const template=data.server_settings.find(s=>s.cadence===cadence);const node=reportSchedule(data,{...template,owner_id:'new',revision:0,next_at:0,config:{...template.config,enabled:false,channel_id:null}},'channel');node.open=true;schedules.append(node);};
    root.append(el('details',{class:'card',id:'report-channel-alerts'},el('summary',{},'Server channel daily and weekly alerts'),
    muted('Manage Server required. These reports always cover the whole server. Daily and weekly reports can use different channels and content.'),
    el('div',{class:'profile-toolbar'},button('Add daily channel schedule',()=>add('daily')),button('Add weekly channel schedule',()=>add('weekly'))),schedules,reportDeliveryHistory(data.server_deliveries)));}
  root.append(muted('Reports use up to 30 days of stored observations. Tracking cannot backfill time before an account was linked. Unknown results are excluded from win rates. MMR is summed per player and playlist; observed times are polling times.'));
  return root;
}
function reportBody(data) {
  const r=data.report,s=r.sections,t=r.totals,root=el('div',{class:'stack report-body'}),metrics=[];
  if(r.retained_partial)root.append(el('p',{class:'note'},'This period extends beyond retained history and may be incomplete.'));
  if(s.includes('results'))metrics.push(datum('Observed games',String(t.games),`${r.active_players} active / ${r.tracked_players} tracked players`),
    datum('Results',`${t.wins}W / ${t.losses}L`,`${t.unknown} uncertain`),datum('Win rate',t.win_rate==null?'—':t.win_rate+'%','Known results only'));
  if(metrics.length)root.append(el('div',{class:'session-metrics','data-section':'results'},metrics));
  if(s.includes('mmr'))root.append(el('div',{class:'session-metrics','data-section':'mmr'},datum('Net MMR',reportMMR(t.mmr),'Sum across player/playlist changes')));
  if(s.includes('comparison')){
    const p=r.previous,items=[];
    if(s.includes('results'))items.push(datum('Game change',`${t.games-p.games>=0?'+':''}${t.games-p.games}`,`${p.games} in the previous period`),
      datum('Win-rate change',t.win_rate==null||p.win_rate==null?'—':`${(t.win_rate-p.win_rate).toFixed(1)} pp`,'Known results only'));
    if(s.includes('mmr'))items.push(datum('MMR change versus previous',t.mmr==null||p.mmr==null?'—':reportMMR(t.mmr-p.mmr),`Previous net: ${reportMMR(p.mmr)}`));
    root.append(card(el('h2',{},'Previous period comparison'),r.comparison_available&&r.complete?el('div',{class:'session-metrics'},items.length?items:muted('Include results or MMR to compare their totals.')):muted(r.complete?'The previous period extends beyond retained history.':'Comparisons are available for completed periods.')));
  }
  const charts=reportCharts(r);if(charts){charts.dataset.section=s.includes('activity')?'activity':'players';root.append(charts);}
  if(s.includes('players'))root.append(reportPlayerTable(r));
  if(s.includes('playlists'))root.append(card(el('h2',{},'Playlist breakdown'),reportStatsTable(r.playlists,s)));
  if(s.includes('activity'))root.append(card(el('h2',{},'Activity by day'),reportStatsTable((r.daily||[]).map(d=>({...d,name:d.date})),s)));
  for(const [category,group] of Object.entries(r.highlights)){
    root.append(card(el('div',{class:'section-top'},el('h2',{},data.sections[category]),el('span',{class:'badge'},`${group.count} events`)),
      group.items.length?reportHighlightRows(category,group.items,data.playlists,r.timezone):muted('No matching events in this period.')));
  }
  const names={'Previous period comparison':'comparison','Player breakdown':'players','Playlist breakdown':'playlists','Activity by day':'activity',...Object.fromEntries(Object.entries(data.sections).map(([key,label])=>[label,key]))};
  const section=n=>n.dataset.section||names[n.querySelector('h2')?.textContent];
  const ordered=[...root.children].filter(n=>section(n)).sort((a,b)=>s.indexOf(section(a))-s.indexOf(section(b)));root.append(...ordered);
  if(!t.games&&!Object.values(r.highlights).some(h=>h.count))root.prepend(empty('No observed activity','Try another period, scope, or history source. Your selected sections are preserved.'));
  return root;
}
function reportHighlightRows(category,items,playlists,timezone) {
  const root=el('div',{class:'stack'}),rows=el('div',{class:'activity-list'}),pager=el('div',{class:'pagination'});let page=0;
  const draw=()=>{
    rows.replaceChildren(...items.slice(page*20,page*20+20).map(i=>el('div',{class:'event-row report-event-row'},
      el('div',{},el('strong',{},i.name),el('p',{},reportHighlight(category,i,playlists))),el('time',{},reportTimestamp(i.at,timezone)))));
    const prev=button('Previous',()=>{page--;draw();}),next=button('Next',()=>{page++;draw();});
    prev.disabled=page===0;next.disabled=(page+1)*20>=items.length;
    pager.replaceChildren(prev,muted(`${items.length} player/playlist groups · page ${page+1} of ${Math.ceil(items.length/20)}`),next);
  };draw();root.append(rows);if(items.length>20)root.append(pager);return root;
}
function reportHighlight(category,i,playlists) {
  const mode=playlists[i.playlist]||(i.scope==='main'?'Competitive':i.scope==='all'?'All ranked':'Across playlists');
  if(category==='peak_mmr')return `${mode} · peak ${i.mmr==null?'unavailable':Math.round(i.mmr)+' MMR'} · ${i.events} new peaks`;
  if(category.includes('streak'))return `${mode} · best completed ${i.best}-game ${i.result||''} streak · ${i.events} events${i.position?' · best entry #'+i.position:''}`;
  return `${mode} · ${i.events} changes${i.rank_before?' · '+i.rank_before+' → '+i.rank_after:''}${i.position?' · best position #'+i.position:''}`;
}
function reportStatsTable(rows,sections) {
  const results=sections.includes('results'),mmr=sections.includes('mmr');
  return table(['Name',...(results?['Games','Wins','Losses','Uncertain','Win rate']:[]),...(mmr?['Net MMR']:[]),...(!results&&!mmr?['Games']:[])],rows.map(p=>[
    p.name,...(results?[p.games,p.wins,p.losses,p.unknown,p.win_rate==null?'—':p.win_rate+'%']:[]),...(mmr?[reportMMR(p.mmr)]:[]),...(!results&&!mmr?[p.games]:[])]));
}
function reportPlayerTable(r) {
  const search=el('input',{type:'search',placeholder:'Find player…','aria-label':'Find report player'});
  const sort=select('Sort players','sort',[{value:'games',label:'Most games'},{value:'gains',label:'Largest MMR gains'},{value:'losses',label:'Largest MMR losses'},{value:'name',label:'Name'}],r.config?.player_order==='activity'?'games':r.config?.player_order||'games');
  const body=el('div'),pager=el('div',{class:'pagination'});let page=0;
  const draw=()=>{
    const order=sort.querySelector('select').value;
    const rows=r.players.filter(p=>p.name.toLowerCase().includes(search.value.toLowerCase())).sort((a,b)=>order==='name'?a.name.localeCompare(b.name):order==='gains'?(b.mmr??-Infinity)-(a.mmr??-Infinity):order==='losses'?(a.mmr??Infinity)-(b.mmr??Infinity):b.games-a.games);
    const pages=Math.max(1,Math.ceil(rows.length/20));page=Math.min(page,pages-1);
    body.replaceChildren(reportStatsTable(rows.slice(page*20,page*20+20),r.sections));
    const previous=button('Previous',()=>{page--;draw();}),next=button('Next',()=>{page++;draw();});previous.disabled=page===0;next.disabled=page===pages-1;
    pager.replaceChildren(previous,muted(`${rows.length} players · page ${page+1} of ${pages}`),next);
  };
  search.addEventListener('input',()=>{page=0;draw();});sort.addEventListener('change',()=>{page=0;draw();});draw();
  return card(el('h2',{},'Player breakdown'),el('div',{class:'profile-toolbar'},search,sort),body,pager);
}
function reportSchedule(data,setting,target) {
  const p={...setting.config,timezone:setting.revision?setting.config.timezone:playerTimezone()},channel=target==='channel',picks=reportSections(data,p,`${target}_${setting.cadence}_${setting.scope}`);
  const fields=[toggle('Enabled','enabled',p.enabled),input('Timezone (IANA)','timezone',{value:p.timezone,required:true,maxlength:100}),
    input('Delivery time','clock',{type:'time',value:`${String(p.hour).padStart(2,'0')}:${String(p.minute).padStart(2,'0')}`,required:true}),
    ...(setting.cadence==='weekly'?[select('Week starts / delivery day','weekday',['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday'].map((label,value)=>({value:String(value),label})),String(p.weekday))]:[]),
    input('Minimum observed games','minimum',{type:'number',min:0,max:10000,value:p.minimum,required:true}),
    input('Highlights per alert section','top',{type:'number',min:1,max:15,value:p.top,required:true}),toggle('Skip empty reports','skip_empty',p.skip_empty),
    ...(channel?[select('Destination channel','channel_id',[{value:'',label:'Choose channel'},...data.channels.map(c=>({value:c.id,label:'#'+c.name}))],p.channel_id||'')]:[]),...reportDetailControls(p),picks.node,
    ...(channel?[el('div',{class:'wide'},toggle('Replace instant channel alerts for included event categories','replace_instant',false),muted('When saved, disables matching instant event routes across this server. Other categories stay unchanged. Later disabling this digest does not restore those routes; use Server admin to re-enable them.'))]:[])];
  const payload=f=>{
      const [hour,minute]=f.elements.clock.value.split(':').map(Number),sections=chosen(picks.sections);
      if(!sections.length)throw Error('Include at least one section.');
      const config={...p,...reportDetails(f),enabled:f.elements.enabled.checked,timezone:f.elements.timezone.value,hour,minute,
        weekday:f.elements.weekday?Number(f.elements.weekday.value):p.weekday,minimum:Number(f.elements.minimum.value),top:Number(f.elements.top.value),
        skip_empty:f.elements.skip_empty.checked,sections,playlists:selectedReportPlaylists(picks.playlists,data),channel_id:channel?(f.elements.channel_id.value||null):null};
      return {guild_id:data.guild_id,cadence:setting.cadence,scope:setting.scope,target,config,schedule_id:channel?(setting.owner_id==='new'?'-'+(config.channel_id||'0'):setting.owner_id):'0',replace_instant:!!f.elements.replace_instant?.checked};
  };
  const preview=el('div',{class:'wide stack'});
  const editor=form(fields,async f=>{await api('/api/reports/settings',payload(f));notify('Summary schedule saved.');await navigate('reports');},'Save schedule');
  const action=async kind=>{try{const result=await api('/api/reports/settings',{...payload(editor),replace_instant:false,action:kind});
    if(kind==='preview')preview.replaceChildren(el('h3',{},'Alert preview · previous completed period'),el('pre',{class:'report-preview'},result.text),reportBody(result));else notify(result.detail);
  }catch(e){notify(e.message);}};
  editor.append(el('div',{class:'profile-toolbar wide'},button('Preview alert',()=>action('preview')),button(channel?'Send test to channel':'Send test to me',()=>action('send_test'))),preview);
  return el('details',{class:'card report-schedule'},el('summary',{},`${setting.cadence==='daily'?'Daily':'Weekly'} · ${channel?'Server channel':setting.scope==='self'?'Just me':'Whole server'} · ${p.enabled?'On':'Off'}${channel&&setting.owner_id!=='0'?' · additional':''}`),
    setting.next_at?muted('Next delivery: '+date(setting.next_at)):muted('Delivery is disabled.'),editor,
    muted('Tests send the previous completed period without saving a schedule or applying minimum-game/empty-report suppression. Minimum streak and peak-gain filters apply.'));
}
function reportDeliveryHistory(rows) {
  return el('details',{},el('summary',{},'Recent delivery history'),rows.length?table(['Report','Period ended','Status','Attempts','Details','Snapshot'],rows.map(r=>[
    `${r.cadence} · ${r.scope}`,date(r.period_end),r.status,r.attempts,
    [r.detail,...Object.entries(JSON.parse(r.receipts||'{}')).map(([k,v])=>`${k}: ${v.status} (${v.detail})`)].filter(Boolean).join(' · '),
    r.has_report?button('View delivered report',()=>{state.reportSnapshot={guild_id:state.reportFilters.guild_id,owner:r.owner_id,delivery:r.id};navigate('reports');}):'—'])):muted('No scheduled reports yet. Any retained daily or weekly period is available above without an alert schedule.'));
}
