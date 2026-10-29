/* Helix 1.1.4 UX additions — "felt-speed" bundle.
 *
 * This file is spliced into index.html at Python-load time (see
 * desktop_app.py::_load_app_html). The SPA runs as a single document in
 * pywebview with no file:// base — relative <script src=…> would not
 * resolve — so we keep the source split for maintainability but assemble
 * at load so the runtime behaviour is unchanged.
 *
 * Contents (new code only, no behaviour change to anything existing):
 *   - "Last updated · Refresh" chip framework
 *   - Sidebar sparkline helper (small SVG strip next to live pills)
 *   - Settings nav rail scrollspy initialiser
 *   - One-time Ollama warmup on first Assistant-page visit
 */

/* ── "Last updated · Refresh" chip framework ────────────────────────────── */
const _lastUpdated = {};
function _fmtAgo(ts){
  const d = Math.floor((Date.now()-ts)/1000);
  const ja = document.documentElement.getAttribute('data-lang')==='ja';
  if(d<5) return ja ? 'たった今' : 'just now';
  if(d<60) return ja ? (d+'秒前') : (d+' s ago');
  if(d<3600) return ja ? (Math.floor(d/60)+'分前') : (Math.floor(d/60)+' min ago');
  return ja ? (Math.floor(d/3600)+'時間前') : (Math.floor(d/3600)+' h ago');
}
function _stampUpdated(key){
  _lastUpdated[key] = Date.now();
  _renderUpdatedChip(key);
}
function _renderUpdatedChip(key){
  const el = document.getElementById('uc-'+key);
  if(!el) return;
  const ts = _lastUpdated[key];
  const lbl = el.querySelector('.uc-time');
  if(!lbl) return;
  if(!ts){ lbl.textContent=''; el.classList.remove('fresh'); return; }
  const lang = document.documentElement.getAttribute('data-lang')||'en';
  const prefix = (lang==='ja') ? '更新 ' : 'Updated ';
  lbl.textContent = prefix + _fmtAgo(ts);
  if(Date.now()-ts<10000) el.classList.add('fresh'); else el.classList.remove('fresh');
}
setInterval(()=>{ Object.keys(_lastUpdated).forEach(_renderUpdatedChip); }, 10000);

/* ── Sidebar sparkline ──────────────────────────────────────────────────── */
function drawSidebarSpark(svgId, data, color){
  const el = document.getElementById(svgId); if(!el) return;
  if(!data || data.length<2){ el.innerHTML=''; return; }
  const w=36, h=14, n=data.length, step=w/(n-1);
  const max = Math.max(100, ...data);
  let d='';
  for(let i=0; i<n; i++){
    const x=i*step, y=h-(data[i]/max)*h;
    d += (i?' L ':'M ') + x.toFixed(1) + ' ' + y.toFixed(1);
  }
  el.innerHTML = '<path d="'+d+'" fill="none" stroke="'+color+'" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"/>';
}

/* ── Settings nav rail scrollspy ────────────────────────────────────────── */
let _setRailObserver = null;
function _initSetRailScrollspy(){
  const rail = document.getElementById('set-rail');
  const content = document.getElementById('set-content');
  if(!rail || !content) return;
  rail.querySelectorAll('.set-rail-link').forEach(a=>{
    if(a.dataset.wired) return;
    a.dataset.wired = '1';
    a.addEventListener('click', ev=>{
      ev.preventDefault();
      const t = document.getElementById(a.dataset.sec);
      if(t) content.scrollTo({top: t.offsetTop-6, behavior:'smooth'});
    });
  });
  if(_setRailObserver) return;
  const ratios = {};
  _setRailObserver = new IntersectionObserver(es=>{
    es.forEach(e=>{ ratios[e.target.id] = e.intersectionRatio; });
    let best='', bestR=-1;
    Object.keys(ratios).forEach(k=>{ if(ratios[k]>bestR){ bestR=ratios[k]; best=k; } });
    if(!best) return;
    rail.querySelectorAll('.set-rail-link').forEach(a=>{
      a.classList.toggle('active', a.dataset.sec===best);
    });
  }, {root: content, threshold: [0,.25,.5,.75,1]});
  rail.querySelectorAll('.set-rail-link').forEach(a=>{
    const t = document.getElementById(a.dataset.sec);
    if(t) _setRailObserver.observe(t);
  });
}

/* ── One-time Ollama warmup on first Assistant open ─────────────────────── */
let _ollamaWarmupFired = false;
function warmupOllamaOnce(){
  if(_ollamaWarmupFired) return;
  _ollamaWarmupFired = true;
  try{
    if(window.pywebview && window.pywebview.api && window.pywebview.api.warmup_ollama){
      window.pywebview.api.warmup_ollama();
    }
  } catch(e){ _ollamaWarmupFired = false; }
}
