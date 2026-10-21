(() => {
  'use strict';
  const DATA = JSON.parse(document.getElementById('data').textContent);
  const $ = id => document.getElementById(id);
  const COLORS = ['#aac59a','#c3d3aa','#e6c999','#b4cbd0','#c5b9d2','#d6b8a4','#a7c9bd','#d5d99a','#d6bac0','#b4c1da','#e5d2b5','#bdd6c2'];
  const PROVIDERS = {aws: 'AWS', gcp: 'GCP', claude: 'Claude'};
  const state = {page: 'aws', entity: null, month: DATA.months.at(-1), currency: 'USD', search: '', provider: 'all'};
  const currencies = [...new Set(DATA.records.map(r => r.currency))].sort();
  state.currency = currencies.includes('USD') ? 'USD' : currencies[0] || 'USD';
  function node(tag, text, cls) {
    const element = document.createElement(tag);
    if (text !== undefined) element.textContent = text;
    if (cls) element.className = cls;
    return element;
  }
  function color(name) { let h = 0; for (const c of name) h = (Math.imul(h,31) + c.charCodeAt(0))|0; return COLORS[(h>>>0)%COLORS.length]; }
  const money = value => new Intl.NumberFormat('en', {style:'currency', currency:state.currency, maximumFractionDigits:2}).format(value);
  const compact = value => new Intl.NumberFormat('en', {style:'currency', currency:state.currency, notation:'compact', maximumFractionDigits:1}).format(value);
  const monthName = month => new Date(month+'-01T00:00:00Z').toLocaleDateString('en', {month:'short', year:'numeric', timeZone:'UTC'});
  const nextMonth = month => { const d = new Date(month+'-01T00:00:00Z'); d.setUTCMonth(d.getUTCMonth()+1); return d.toISOString().slice(0,10); };
  const partial = month => DATA.end < nextMonth(month);
  const sum = rows => rows.reduce((a,r)=>a+r.amount,0);
  const change = (current,previous) => current !== null && previous !== null && previous > 0 ? (current-previous)/previous*100 : null;
  const changeText = n => n === null ? '—' : `${n>=0?'+':''}${n.toFixed(1)}%`;
  function scoped(search = true) {
    return DATA.records.filter(r => r.currency === state.currency &&
      (state.page === 'trends' ? state.provider === 'all' || r.provider === state.provider : r.provider === state.page) &&
      (!state.entity || r.entity_id === state.entity) && (!search || r.service.toLowerCase().includes(state.search.toLowerCase())));
  }
  function groups(rows) {
    const services = new Map();
    rows.forEach(r => services.set(r.service, (services.get(r.service)||0)+r.amount));
    return [...services].map(([name,amount])=>({name,amount})).sort((a,b)=>b.amount-a.amount);
  }
  function link(text, hash, cls) {
    const a = node('a', text, cls); a.href = hash;
    if (location.hash === hash || (!location.hash && hash === '#aws')) { a.classList.add('active'); a.setAttribute('aria-current','page'); }
    return a;
  }
  function nav() {
    const nav = $('nav'); nav.replaceChildren(node('div','PROVIDERS','nav-label'));
    const month = state.page === 'trends' ? DATA.months.at(-1) : state.month;
    for (const [provider,label] of Object.entries(PROVIDERS)) {
      const wrap = node('div', undefined, 'nav-group');
      const a = link(undefined,'#'+provider,'nav-link');
      const providerRows = DATA.records.filter(r=>r.provider===provider && r.month===month && r.currency===state.currency);
      const providerLabel = node('span', undefined, 'nav-provider-label');
      providerLabel.append(node('span', {aws: 'Amazon Web Services', gcp: 'Google Cloud', claude: 'Claude'}[provider]),
        node('small', `${monthName(month)} · ${providerRows.length ? money(sum(providerRows)) : 'No data'}`));
      a.append(node('span',{aws:'a',gcp:'G',claude:'✳'}[provider],'provider-icon'), providerLabel);
      wrap.append(a);
      const entities = new Map(DATA.records.filter(r=>r.provider===provider).map(r=>[r.entity_id,r.entity_name]));
      const costs = new Map();
      for (const row of DATA.records) {
        if (row.provider === provider && row.month === month && row.currency === state.currency) {
          costs.set(row.entity_id, (costs.get(row.entity_id) || 0) + row.amount);
        }
      }
      // Rank by total net cost, independent of the account and service filters.
      const ranked = [...entities].sort((a,b) =>
        Number(costs.has(b[0])) - Number(costs.has(a[0])) ||
        (costs.get(b[0]) || 0) - (costs.get(a[0]) || 0) || a[1].localeCompare(b[1]));
      for (const [id,name] of ranked) {
        const child = link(undefined, '#'+provider+'/'+encodeURIComponent(id),'nav-link nav-child');
        child.title = `${name} · ${id} · ${monthName(month)}: ${costs.has(id) ? money(costs.get(id)) : 'No data'}`;
        const line = node('span', undefined, 'nav-entity');
        line.append(node('span',name,'nav-name'), node('span', costs.has(id) ? money(costs.get(id)) : '—', 'nav-cost'));
        child.append(line);
        if (provider !== 'claude') child.append(node('small',id));
        wrap.append(child);
      }
      nav.append(wrap);
    }
    nav.append(node('div','ANALYSIS','nav-label'), link('↗  Monthly trends','#trends','nav-link'));
  }
  function stat(label, value, note, cls='') {
    const card = node('div',undefined,'stat'); card.append(node('div',label,'stat-label'),node('div',value,'stat-value '+cls),node('div',note,'stat-note')); return card;
  }
  // Balanced recursive subdivision gives each rectangle exactly its cost share.
  function layout(items,x,y,w,h) {
    if (!items.length) return [];
    if (items.length===1) return [{...items[0],x,y,w,h}];
    const total=items.reduce((a,r)=>a+r.amount,0);
    let index=1,left=items[0].amount;
    while(index<items.length-1 && Math.abs(left+items[index].amount-total/2)<Math.abs(left-total/2)) left+=items[index++].amount;
    const ratio=left/total;
    return w>=h ? [...layout(items.slice(0,index),x,y,w*ratio,h),...layout(items.slice(index),x+w*ratio,y,w*(1-ratio),h)] :
      [...layout(items.slice(0,index),x,y,w,h*ratio),...layout(items.slice(index),x,y+h*ratio,w,h*(1-ratio))];
  }
  function filterService(name) { state.search = state.search === name ? '' : name; $('search').value=state.search; render(); }
  function treemap(services) {
    const chart=$('chart');chart.replaceChildren();
    const positive=services.filter(s=>s.amount>0), total=sum(positive);
    if (!total) { chart.append(node('div', services.length?'No positive costs to map. Credits and adjustments appear below.':'No billing rows match this view. Try another month or clear the filters.','empty')); return; }
    const map=node('div',undefined,'treemap');map.setAttribute('aria-label','Services sized by positive cost');chart.append(map);
    const rect=map.getBoundingClientRect();
    for(const item of layout(positive,0,0,rect.width,rect.height)) {
      const tile=node('button',undefined,'tile'+(item.w<65||item.h<34?' micro':item.w<135||item.h<85?' tiny':''));
      Object.assign(tile.style,{left:item.x+'px',top:item.y+'px',width:item.w+'px',height:item.h+'px',background:color(item.name)});
      const share=item.amount/total*100;
      const desc=`${item.name}: ${money(item.amount)} · ${share.toFixed(1)}% of positive spend. Click to filter.`;
      tile.title=desc;tile.setAttribute('aria-label',desc);
      tile.append(node('strong',item.name),node('span',money(item.amount),'tile-value'),node('small',share.toFixed(1)+'% of positive spend'));
      tile.onclick=()=>filterService(item.name); map.append(tile);
    }
  }
  function headings(labels) {
    const row=node('tr');labels.forEach((label,i)=>row.append(node('th',label,i?'num':'')));$('thead').replaceChildren(row);$('tbody').replaceChildren();
  }
  function breakdown(services,rows) {
    headings([state.page === 'claude' ? 'Model / usage' : 'Service','Cost','Share of positive spend','Previous month','Change']);
    const index=DATA.months.indexOf(state.month), previous=index>0?DATA.months[index-1]:null;
    const baseline=scoped(false);const hasPrevious=previous && baseline.some(r=>r.month===previous);
    const previousServices=new Map(groups(rows.filter(r=>r.month===previous)).map(s=>[s.name,s.amount]));
    const positiveTotal=services.filter(s=>s.amount>0).reduce((n,s)=>n+s.amount,0);
    for (const service of services) {
      const tr=node('tr'), td=node('td'), button=node('button',service.name,'service-button');
      const swatch=node('span',undefined,'swatch');swatch.style.background=color(service.name);td.append(swatch,button);button.onclick=()=>filterService(service.name);
      const pct=service.amount>0?service.amount/positiveTotal*100:null;
      const share=node('td',pct===null?'Credit / adjustment':pct.toFixed(1)+'%','num');
      if(pct!==null){const track=node('span',undefined,'share-track'),fill=node('i');fill.style.width=pct+'%';track.append(fill);share.append(track);}
      const prev=hasPrevious?(previousServices.get(service.name)||0):null, delta=change(service.amount,prev);
      tr.append(td,node('td',money(service.amount),'num'),share,node('td',prev===null?'—':money(prev),'num'),node('td',changeText(delta),'num '+(delta>0?'up':delta<0?'down':'')));$('tbody').append(tr);
    }
    if (!services.length) { const td=node('td','No service data for this selection.');td.colSpan=5;$('tbody').append(td); }
  }
  function monthly(rows, month, provider) {
    // Missing provider-month/currency coverage is unknown, rather than fabricated zero.
    const base=scoped(false).filter(r=>r.month===month && (!provider||r.provider===provider));
    return base.length ? sum(rows.filter(r=>r.month===month && (!provider||r.provider===provider))) : null;
  }
  function trendChange(rows, month, previous) {
    if (!previous) return null;
    const providers = Object.keys(DATA.providers).filter(p=>state.provider==='all'||state.provider===p);
    if (providers.some(p=>(monthly(rows,month,p)===null)!==(monthly(rows,previous,p)===null))) return null;
    return change(monthly(rows,month),monthly(rows,previous));
  }
  function svgElement(tag, attrs={}, text) {
    const e=document.createElementNS('http://www.w3.org/2000/svg',tag);
    Object.entries(attrs).forEach(([key,val])=>e.setAttribute(key,val));if(text!==undefined)e.textContent=text;return e;
  }
  function trendChart(rows) {
    const chart=$('chart');chart.replaceChildren();
    const providers=Object.keys(DATA.providers).filter(p=>state.provider==='all'||state.provider===p);
    const series=DATA.months.map(month=>({month,values:providers.map(p=>monthly(rows,month,p)),total:monthly(rows,month)}));
    if (!series.some(s=>s.total!==null)) { chart.append(node('div','No billing data for this selection.','empty'));return; }
    const colors={aws:'#ff9900',gcp:'#4285f4',claude:'#d97757'}, legend=node('div',undefined,'trend-legend');
    providers.forEach(p=>{const label=node('span'),swatch=node('span',undefined,'swatch');swatch.style.background=colors[p];label.append(swatch,node('span',PROVIDERS[p]));legend.append(label);});chart.append(legend);
    const svg=svgElement('svg',{viewBox:'0 0 960 330',role:'img','aria-label':'Monthly costs by provider; exact values in the table below',class:'trend-svg'});
    svg.append(svgElement('title',{},'Monthly cloud costs by provider'));
    const max=Math.max(1,...series.map(s=>s.values.reduce((a,v)=>a+Math.max(0,v||0),0)))*1.22;
    const min=Math.min(0,...series.map(s=>s.values.reduce((a,v)=>a+Math.min(0,v||0),0)))*1.25;
    const y=v=>270-(v-min)/(max-min)*240, step=850/series.length;
    for(let i=0;i<=4;i++){const value=min+(max-min)*i/4;svg.append(svgElement('line',{x1:80,x2:935,y1:y(value),y2:y(value),stroke:'#e9ede4'}),svgElement('text',{x:70,y:y(value)+4,'text-anchor':'end'},compact(value)));}
    svg.append(svgElement('line',{x1:80,x2:935,y1:y(0),y2:y(0),stroke:'#ccd5c4'}));
    series.forEach((s,i)=>{
      const x=85+i*step+step*.2,width=step*.6;let pos=0,neg=0;
      s.values.forEach((value,j)=>{
        if(value===null)return;
        const from=value>=0?pos:neg,to=from+value;
        const rect=svgElement('rect',{x,y:Math.min(y(from),y(to)),width,height:Math.abs(y(to)-y(from)),fill:colors[providers[j]],rx:3,opacity:partial(s.month)?.65:1});
        rect.append(svgElement('title',{},`${monthName(s.month)} · ${PROVIDERS[providers[j]]}: ${money(value)}`));svg.append(rect);
        if(value>=0)pos=to;else neg=to;
      });
      const incomplete=s.values.some(v=>v===null);
      svg.append(svgElement('text',{x:x+width/2,y:y(pos)-12,'text-anchor':'middle',class:'bar-label'},s.total===null?'No data':compact(s.total)+(incomplete?' *':'')),svgElement('text',{x:x+width/2,y:296,'text-anchor':'middle'},monthName(s.month)),svgElement('text',{x:x+width/2,y:313,'text-anchor':'middle'},partial(s.month)?'MONTH TO DATE':''));
    });
    chart.append(svg,node('p','Month-to-date bars are faded. * marks a total with missing provider data. Changes are unavailable when provider coverage differs between months.','trend-note'));
    headings(['Month',...providers.map(p=>PROVIDERS[p]),'Available total','Month over month']);
    series.forEach((s,i)=>{
      const tr=node('tr');tr.append(node('td',monthName(s.month)+(partial(s.month)?' · MTD':'')));
      s.values.forEach(v=>tr.append(node('td',v===null?'—':money(v),'num')));
      tr.append(node('td',s.total===null?'—':money(s.total)+(s.values.some(v=>v===null)?' *':''),'num'));
      const delta=trendChange(rows,s.month,i?series[i-1].month:null);
      tr.append(node('td',changeText(delta),'num '+(delta>0?'up':delta<0?'down':'')));$('tbody').append(tr);
    });
  }
  function render() {
    const trends=state.page==='trends',rows=scoped(),current=rows.filter(r=>r.month===state.month),services=groups(current);
    const claude=state.page==='claude';
    $('search-caption').textContent=claude?'Filter models':trends?'Filter services / models':'Filter services';
    $('search').placeholder=claude?'Search a model…':'Search a service…';
    $('provider-filter').hidden=!trends;$('month').disabled=trends;
    const notices=[];
    if(DATA.demo)notices.push('SAMPLE DATA — illustrative costs only. Run make extract to replace this preview with your billing data.');
    if(!trends&&!DATA.providers[state.page])notices.push(`No ${PROVIDERS[state.page]} snapshot has been extracted.`);
    if(trends&&Object.keys(DATA.providers).length<Object.keys(PROVIDERS).length)notices.push('Totals include only providers with saved snapshots.');
    const claudeMeta=DATA.providers.claude;
    const missingClaude=claudeMeta?.missing_months||[];
    if(claude&&claudeMeta&&missingClaude.includes(state.month))notices.push(`No Claude data has been imported for ${monthName(state.month)}. ${claudeMeta.covered_months?.length ? 'Latest available month: '+monthName(claudeMeta.covered_months.at(-1))+'.' : ''}`);
    if(trends&&state.provider!=='aws'&&state.provider!=='gcp'&&missingClaude.length)notices.push(`Claude data is unavailable for ${missingClaude.map(monthName).join(', ')}. Combined totals for those months cover available providers only.`);
    if(partial(trends?DATA.months.at(-1):state.month))notices.push('Current month is month-to-date. Changes against a complete previous month are partial comparisons.');
    if(!trends&&DATA.providers[state.page]?.estimated_months?.includes(state.month))notices.push('AWS marks this month’s costs as estimated.');
    $('notice').textContent=notices.join(' ');
    const stats=$('stats');stats.replaceChildren();
    if(trends){
      const totals=DATA.months.map(m=>monthly(rows,m)),known=totals.filter(v=>v!==null),latest=totals.at(-1),delta=trendChange(rows,DATA.months.at(-1),DATA.months.length>1?DATA.months.at(-2):null);
      stats.append(stat('Latest month',latest===null?'—':money(latest),monthName(DATA.months.at(-1))+(partial(DATA.months.at(-1))?' · month to date':'')),stat('Month over month',changeText(delta),'Latest month vs previous month',delta>0?'up':delta<0?'down':''),stat('Period total',known.length?money(known.reduce((a,b)=>a+b,0)):'—',known.length+' months with billing rows'),stat('Providers',String(Object.keys(DATA.providers).filter(p=>state.provider==='all'||p===state.provider).length),'Currencies are never combined'));
      $('chart-title').textContent='How spend is changing';$('chart-caption').textContent='Monthly costs, stacked by provider. Hover a bar for details.';$('chart-tag').textContent=state.currency+' · monthly';$('table-title').textContent='Monthly breakdown';$('table-caption').textContent='Missing coverage is shown as —. Cloud costs include exported credits; Claude shows API usage cost.';trendChart(rows);
    }else{
      const idx=DATA.months.indexOf(state.month),previous=idx>0?monthly(rows,DATA.months[idx-1]):null,total=monthly(rows,state.month),delta=change(total,previous),positive=services.filter(s=>s.amount>0),credits=services.filter(s=>s.amount<0).reduce((a,s)=>a+s.amount,0);
      stats.append(stat(claude?'API usage cost':'Net cost',total===null?'—':money(total),claude?'Reported CSV usage cost; not an invoice or payment total':credits?`${money(credits)} in net-negative services`:'Includes signed costs and adjustments'),stat('Month over month',changeText(delta),idx>0?'Compared with '+monthName(DATA.months[idx-1]):'No earlier month in this snapshot',delta>0?'up':delta<0?'down':''),stat(claude?'Largest model / usage':'Largest service',positive.length?money(positive[0].amount):'—',positive[0]?.name||'No positive costs'),stat({aws:'Accounts',gcp:'Projects',claude:'Workspaces'}[state.page],String(new Set(current.map(r=>r.entity_id)).size),services.length+(claude?' model / usage categories':' services')+' in this view'));
      $('chart-title').textContent='Where the money goes';$('chart-caption').textContent=claude?'Rectangle area represents reported API usage cost. Click a model to filter.':'Rectangle area represents positive net service cost. Click a service to filter.';$('chart-tag').textContent=monthName(state.month)+(partial(state.month)?' · MTD':'');$('table-title').textContent=claude?'Model / usage breakdown':'Service breakdown';$('table-caption').textContent=claude?'Models and additional usage charges, grouped by workspace.':'All service costs, including credits. Click a service name to filter.';treemap(services);breakdown(services,rows);
    }
    const through=new Date(DATA.end+'T00:00:00Z');through.setUTCDate(through.getUTCDate()-1);
    $('footnote').textContent=`Usage dates in UTC · ${DATA.start} through ${through.toISOString().slice(0,10)} (end exclusive: ${DATA.end}). AWS: ${DATA.providers.aws?.metric||'not extracted'}. GCP: cost + credits. Claude: direct API usage from imported CSVs; separate from Claude charges billed through AWS/GCP. Billing data may arrive late or change; an empty month is not proof of zero spend. No currency conversion.`;
    nav();
  }
  function route() {
    const [page,encoded]=location.hash.slice(1).split('/');state.page=[...Object.keys(PROVIDERS),'trends'].includes(page)?page:'aws';
    try{state.entity=encoded&&state.page!=='trends'?decodeURIComponent(encoded):null;}catch{state.entity=null;}
    state.search='';$('search').value='';render();
  }
  DATA.months.forEach(month=>{const option=node('option',monthName(month)+(partial(month)?' · MTD':''));option.value=month;$('month').append(option);});$('month').value=state.month;
  (currencies.length?currencies:['USD']).forEach(currency=>{const option=node('option',currency);option.value=currency;$('currency').append(option);});$('currency').value=state.currency;
  $('mode').textContent=DATA.demo?'Sample workspace':'Billing workspace';$('mode').classList.toggle('sample',DATA.demo);
  $('snapshot').textContent=Object.entries(DATA.providers).map(([p,meta])=>PROVIDERS[p]+': '+new Date(meta.extracted_at||DATA.generated_at).toLocaleString()).join(' · ');
  $('month').onchange=e=>{state.month=e.target.value;render();};$('currency').onchange=e=>{state.currency=e.target.value;render();};$('provider').onchange=e=>{state.provider=e.target.value;render();};$('search').oninput=e=>{state.search=e.target.value;render();};
  $('reset').onclick=()=>{state.search='';state.provider='all';state.month=DATA.months.at(-1);state.currency=currencies.includes('USD')?'USD':currencies[0]||'USD';$('search').value='';$('provider').value='all';$('month').value=state.month;$('currency').value=state.currency;render();};
  window.addEventListener('hashchange',route);
  let timer;window.addEventListener('resize',()=>{clearTimeout(timer);timer=setTimeout(render,100);});
  route();
})();
