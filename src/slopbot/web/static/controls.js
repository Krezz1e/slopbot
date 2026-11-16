'use strict';
function personalSessionPanel(guild,p){
  return settingsPanel('Play session summaries','Personal boundaries in this server. New cutoffs apply to the next session. Summaries use your private delivery preference.',
    sessionSettingsFields(p),guild,'sessionpreferences',f=>({cutoff_minutes:Number(f.elements.cutoff.value),enabled:f.elements.enabled.checked,minimum_games:Number(f.elements.minimum.value),include_game_observations:f.elements.include_observations.checked}));
}
function serverSessionPanel(guild,p){
  return settingsPanel('Server play sessions','All playlists share a session. This cutoff also controls server session streaks. New cutoffs apply to the next session; all-time streaks continue across sessions.',
    [...sessionSettingsFields(p),select('Summary channel','channel',[{value:'',label:'Choose channel'},...guild.channels.filter(c=>c.type===0).map(c=>({value:c.id,label:'#'+c.name}))],p.channel_id||'')],
    guild,'setup sessions',f=>{
      const result={cutoff_minutes:Number(f.elements.cutoff.value),enabled:f.elements.enabled.checked,minimum_games:Number(f.elements.minimum.value),include_game_observations:f.elements.include_observations.checked};
      if(f.elements.channel.value)result.channel=f.elements.channel.value;
      if(result.enabled&&!result.channel)throw Error('Choose a summary channel.');return result;
    });
}
function toggle(label,name,checked){return el('label',{class:'check-inline'},el('input',{type:'checkbox',name,checked}),label);}
function chosen(node){return [...node.querySelectorAll('input[type="checkbox"]:checked')].map(n=>n.value);}
function channelChecks(channels,selected=[]){return el('div',{class:'channel-checks'},channels.map(c=>el('label',{class:'check-inline'},el('input',{type:'checkbox',value:c.id,checked:selected.includes(c.id)}),'#'+c.name)));}
async function settingsContext(admin=false){
  if(!state.me.user.discord_id)return {empty:card(el('h2',{},'Link Discord first'),muted('These settings control your Discord notifications and servers.'),button('Connect Discord',()=>navigate('account')))};
  const guilds=(await loadGuilds()).filter(g=>!admin||g.admin);
  if(!guilds.length)return {empty:empty(admin?'No servers to administer':'No shared servers',admin?'Manage Server permission is required.':'Join a server where Slopbot is installed.')};
  const id=guilds.find(g=>g.id===state.settingsGuild)?.id||guilds[0].id;
  state.settingsGuild=id;
  const [guild,settings]=await Promise.all([api('/api/guild/'+id),api('/api/settings/'+id)]);
  const selector=select('Server','server',guilds.map(g=>({value:g.id,label:g.name})),id);
  selector.addEventListener('change',e=>{state.settingsGuild=e.target.value;navigate(state.page);});
  return {guild,settings,selector};
}
async function settingsCommand(guild,name,values,target){
  const channel=guild.channels.find(c=>c.type===0)||guild.channels[0];
  if(!channel)throw Error('No accessible Discord channel is available for this action.');
  state.guild=guild;
  const result=await api('/api/command',{command:name,guild_id:guild.id,channel_id:channel.id,values});
  renderFlow(target,result);
}
function settingsPanel(title,description,fields,guild,command,values){
  const output=el('div',{'aria-live':'polite'});
  return card(el('h2',{},title),muted(description),form(fields,f=>settingsCommand(guild,command,values(f),output)),output);
}
function advancedSetting(guild,command,label,description){
  const output=el('div'),open=button('Configure',async()=>{try{await settingsCommand(guild,command,{},output);}catch(e){errorAt(output,e);}});
  return card(el('h2',{},label),muted(description),open,output);
}
function pushApplicationKey(value){
  const normalized=value.replaceAll('-','+').replaceAll('_','/'),padding='='.repeat((4-normalized.length%4)%4),raw=atob(normalized+padding);
  return Uint8Array.from(raw,char=>char.charCodeAt(0));
}
async function pushDevicePanel(){
  const status=await api('/api/push'),box=card(el('h2',{},'PWA push devices'));
  if(!status.configured){box.append(muted('PWA push is not configured on this Slopbot installation.'));return box;}
  if(!('serviceWorker' in navigator)||!('PushManager' in window)||!('Notification' in window)){box.append(muted('This browser does not support PWA push notifications.'));return box;}
  const registration=await navigator.serviceWorker.ready,current=await registration.pushManager.getSubscription();
  box.append(muted(`${status.devices} subscribed device${status.devices===1?'':'s'} on this account. Permission on this device: ${Notification.permission}.`));
  if(current){box.append(button('Disable on this device',async()=>{await api('/api/push',{action:'unsubscribe',endpoint:current.endpoint});await current.unsubscribe();notify('PWA push disabled on this device.');await navigate('notifications');},'danger'));}
  else{box.append(button('Enable on this device',async()=>{let subscription;try{subscription=await registration.pushManager.subscribe({userVisibleOnly:true,applicationServerKey:pushApplicationKey(status.public_key)});const json=subscription.toJSON();await api('/api/push',{action:'subscribe',endpoint:subscription.endpoint,keys:json.keys});}catch(error){if(subscription)await subscription.unsubscribe();throw error;}notify('PWA push enabled on this device.');await navigate('notifications');},'primary'));}
  return box;
}
async function statusPage(){
  const data=await api('/api/status');
  return el('div',{class:'stack'},heading('Slopbot status','The same service checks used by the bot.',button('Refresh',()=>navigate('status'))),
    card(el('h2',{},data.stale?'Waiting for a recent check':data.healthy?'All monitored services operational':'Some services need attention'),
      muted(data.checked_at?'Last checked '+date(data.checked_at)+' · automatic checks every 2 minutes':'The first background check has not finished.'),
      el('span',{class:'badge'},data.discord?'Discord connected':'Discord reconnecting')),
    el('div',{class:'grid two'},data.lines.map(line=>card(rich(line)))),
    card(muted('Personal issue and recovery alerts are available to server administrators.'),button('Notification settings',()=>navigate('notifications'))));
}
async function adminPage(){
  const context=await settingsContext(true);if(context.empty)return context.empty;
  const {guild,settings:s,selector}=context,text=guild.channels.filter(c=>c.type===0);
  if(!s.admin)return empty('Permission changed','Manage Server is required.');
  const policies=el('div',{class:'stack'}),search=el('input',{type:'search',placeholder:'Find a command…','aria-label':'Find a command policy'});
  const rows=Object.entries(s.commands).map(([key,label])=>{
    const p=s.policies.find(p=>p.command_name===key)||{default_visibility:'public',channel_restricted:false,channel_ids:[]};
    const mode=select('Allowed in','mode',[{value:'all',label:'Every text channel'},{value:'selected',label:'Selected channels'},{value:'none',label:'Blocked in Discord'}],p.channel_restricted?(p.channel_ids.length?'selected':'none'):'all');
    const checks=channelChecks(text,p.channel_ids),visibility=select('Default Discord response','visibility',[{value:'public',label:'Public'},{value:'private',label:'Only the requester'}],p.default_visibility);
    const draw=()=>{checks.hidden=mode.querySelector('select').value!=='selected';};draw();mode.addEventListener('change',draw);
    const box=el('details',{class:'card policy-row'},el('summary',{},el('strong',{},label),el('span',{class:'badge'},p.channel_restricted?(p.channel_ids.length+' allowed channels'):'Unrestricted')),
      form([mode,visibility,el('div',{class:'wide'},checks)],async f=>{
        const selected=f.elements.mode.value,ids=selected==='selected'?chosen(checks):[];
        if(selected==='selected'&&p.channel_ids.some(id=>!text.some(c=>c.id===id)))throw Error('This policy contains channels you cannot access. Manage it in Discord or explicitly choose unrestricted or blocked.');
        if(selected==='selected'&&!ids.length)throw Error('Choose a channel or explicitly select Blocked in Discord.');
        await api('/api/settings/'+guild.id+'/policy',{command:key,visibility:f.elements.visibility.value,restricted:selected!=='all',channels:ids});
        box.querySelector('.badge').textContent=selected==='all'?'Unrestricted':ids.length+' allowed channels';notify('Discord command policy saved.');
      }));box.dataset.search=label.toLowerCase();return box;
  });policies.append(...rows);search.addEventListener('input',()=>rows.forEach(r=>r.hidden=!r.dataset.search.includes(search.value.toLowerCase())));
  const events=el('div',{class:'grid two'},Object.entries(s.event_labels).map(([key,label])=>{
    const current=s.events.find(e=>e[0]===key),streak=['session_streak','all_time_streak'].includes(key);
    const fields=[toggle('Enabled','enabled',!!current),select('Destination channel','channel',[{value:'',label:'Choose channel'},...text.map(c=>({value:c.id,label:'#'+c.name}))],current?.[1]||'')];
    if(streak)fields.push(input('Minimum games','minimum',{type:'number',min:1,max:100,value:current?.[2]||3}));
    if(key==='session_streak')fields.push(input('Session inactivity (minutes)','inactivity',{type:'number',min:5,max:1440,value:s.tracking.inactivity_seconds/60}));
    return settingsPanel(label,'Server announcements use this destination.',fields,guild,'setup events notifications',f=>{
      const v={event:key,enabled:f.elements.enabled.checked};if(f.elements.channel.value)v.channel=f.elements.channel.value;
      if(v.enabled&&!v.channel)throw Error('Choose a destination channel.');
      if(streak)v.minimum=Number(f.elements.minimum.value);if(key==='session_streak')v.inactivity_minutes=Number(f.elements.inactivity.value);return v;
    });
  }));
  const status=settingsPanel('Service announcements','Enable or disable issue and recovery announcements for a channel.',[
    select('Channel','channel',text.map(c=>({value:c.id,label:'#'+c.name})),s.status_channels[0]||''),toggle('Enabled','enabled',true)
  ],guild,'slopbotstatuschannel',f=>({channel:f.elements.channel.value,enabled:f.elements.enabled.checked}));
  status.insertBefore(muted('Currently enabled: '+(s.status_channels.map(id=>'#'+(text.find(c=>c.id===id)?.name||'unavailable channel')).join(', ')||'none')),status.children[2]);
  return el('div',{class:'stack'},heading('Server admin','Discord command permissions and server announcements.'),selector,
    card(el('h2',{},'Command permissions'),muted('These restrictions affect Discord commands. Website results remain private and available to authorized server members.'),search,policies),
    serverSessionPanel(guild,s.server_sessions),card(el('h2',{},'Daily and weekly channel summaries'),muted('Choose schedules, channels, and report sections for server digests.'),button('Configure report alerts',()=>{state.reportFilters={guild_id:guild.id,scope:'server'};navigate('reports');})),
    el('h2',{},'Rocket League announcements'),events,status,
    settingsPanel('Server tournament reminders',s.tournament_channels.length+' configured channel(s). Choose a channel to configure region, timing and game modes.',[select('Destination channel','channel',text.map(c=>({value:c.id,label:'#'+c.name})))],guild,'tournamentchannel',f=>({channel:f.elements.channel.value})),
    button('All server tools',()=>navigate('server')));
}
async function notificationsPage(){
  const context=await settingsContext();if(context.empty)return context.empty;
  const {guild,settings:s,selector}=context,endpoint='/api/settings/'+guild.id+'/personal';
  const save=async data=>{await api(endpoint,data);notify('Notification settings saved.');};
  const delivery=settingsPanel('Private delivery','Applies to your notifications in this server. Telegram and PWA device links are shared across servers.',[
    select('Send alerts through','delivery',[{value:'discord',label:'Discord'},{value:'telegram',label:'Telegram'},{value:'pwa',label:'PWA push'},{value:'both',label:'Discord and Telegram'},{value:'discord_pwa',label:'Discord and PWA push'},{value:'telegram_pwa',label:'Telegram and PWA push'},{value:'all',label:'Discord, Telegram, and PWA push'}],s.delivery)
  ],guild,'notifications',f=>({delivery:f.elements.delivery.value}));
  delivery.append(muted('Telegram '+(s.telegram_linked?'linked':'not linked')+'. Enable this browser below before choosing a PWA push option.'),button('Send personal test',async()=>{const output=el('div');delivery.append(output);try{await settingsCommand(guild,'testnotification',{},output);}catch(e){errorAt(output,e);}},'small'));
  const push=await pushDevicePanel();
  const sessions=personalSessionPanel(guild,s.sessions);sessions.append(button('Daily and weekly summaries',()=>{state.reportFilters={guild_id:guild.id,scope:'self'};navigate('reports');}));
  const invitations=settingsPanel('Invitations to play','Choose whether friends can send you get-on notifications.',[toggle('Receive invitations','enabled',s.geton)],guild,'getonnotifications',f=>({enabled:f.elements.enabled.checked}));
  const kw=s.keywords,checks=channelChecks(guild.channels.filter(c=>[0,5].includes(c.type)),kw.channel_ids);
  const terms=el('div',{class:'row'},kw.keywords.map(term=>button(term+' ×',async()=>{try{await save({action:'keyword_remove',keyword:term});await navigate('notifications');}catch(e){errorAt(terms,e);}},'small')));
  const keywords=card(el('h2',{},'Keyword alerts'),muted('Choose words and the channels to watch. No selected channels means all accessible text channels.'),
    form([toggle('Enabled','enabled',kw.enabled),select('When to notify','mode',[{value:'always',label:'Always'},{value:'offline_only',label:'Only while offline'}],kw.notification_mode),el('div',{class:'wide'},checks)],f=>save({action:'keyword_options',enabled:f.elements.enabled.checked,mode:f.elements.mode.value,channels:chosen(checks)})),terms,
    form([input('Add keyword','keyword',{required:true,maxlength:50})],async f=>{await save({action:'keyword_add',keyword:f.elements.keyword.value});await navigate('notifications');},'Add keyword'));
  const voiceChannels=guild.channels.filter(c=>[2,13].includes(c.type));
  const voice=card(el('h2',{},'Voice channel alerts'),muted('Create or update an alert for a channel. Your saved thresholds appear below.'),
    ...s.voice.map(v=>el('div',{class:'section-top'},muted('#'+(voiceChannels.find(c=>c.id===v.channel_id)?.name||'Unavailable')+' · '+v.threshold+' people · '+[v.notify_on_reach?'reached':null,v.notify_on_drop?'dropped':null].filter(Boolean).join(' / ')),button('Remove',async()=>{try{await save({action:'voice_remove',id:v.id});await navigate('notifications');}catch(e){errorAt(voice,e);}},'small danger'))));
  if(voiceChannels.length)voice.append(form([
    select('Voice channel','channel',voiceChannels.map(c=>({value:c.id,label:c.name}))),input('People threshold','threshold',{type:'number',min:2,max:25,value:3,required:true}),toggle('Threshold reached','reach',true),toggle('Below threshold','drop',false)
  ],async f=>{await save({action:'voice',channel:f.elements.channel.value,threshold:Number(f.elements.threshold.value),reach:f.elements.reach.checked,drop:f.elements.drop.checked});await navigate('notifications');},'Save channel alert'));
  const root=el('div',{class:'stack'},heading('Your notifications','Your existing private preferences, organized in one place.'),selector,el('div',{class:'grid two'},delivery,push,sessions,invitations),keywords,voice,
    advancedSetting(guild,'tournamentalerts','Tournament reminders',s.tournament?'Enabled · '+s.tournament.region+' · '+s.tournament.lead_minutes+' minutes before start':'Not enabled. Choose region, lead time, team sizes and game modes.'),
    advancedSetting(guild,'itemshop watches','Item shop watches',s.watches.length+' saved watches. Review and manage your existing watches.'),
    button('Create a shop watch',()=>{state.guild=guild;state.feature='itemshop watch';navigate('server');}));
  if(s.admin)root.append(settingsPanel('Service issue and recovery alerts','Personal administrator alerts follow your private delivery setting.',[toggle('Receive status alerts','enabled',s.status_alerts)],guild,'slopbotstatusalerts',f=>({enabled:f.elements.enabled.checked})));
  return root;
}
