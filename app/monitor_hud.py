"""PC Health Agent — System monitor HUD overlay.

Compact always-on-top window: CPU ring + sparkline, RAM/Disk bars,
Network stats, and live top-processes panel. Header is draggable.
"""
import threading
import time

_hud_win = None
_hud_api = None
_lock    = threading.Lock()

HUD_TITLE = "Helix Monitor"
HUD_W, HUD_H = 400, 530

# ── HTML ──────────────────────────────────────────────────────────────────────

MONITOR_HUD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Helix Monitor</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{
  --bg:#07090D;--card:#0E1219;--inner:#131820;--hdr:#0A0D13;
  --border:#1C2330;--border2:#1A2130;
  --teal:#00D4AA;--amber:#F5A524;--blue:#5B9CF6;--red:#F06B6B;--purple:#A78BFA;
  --fg:#DDE4F0;--fg2:#7A8AA0;--fg3:#3A4455;
  --mono:'Cascadia Code','Consolas',monospace;
}
html,body{width:100%;height:100%;overflow:hidden;background:var(--bg);
  color:var(--fg);font-family:'Segoe UI',system-ui,sans-serif;font-size:13px}
body{border:1px solid var(--border);border-radius:0}

/* ── Header ── */
.hdr{height:44px;background:var(--hdr);border-bottom:1px solid var(--border);
  padding:0 8px 0 12px;display:flex;align-items:center;gap:8px;
  cursor:default;user-select:none}
.hdr-drag{display:flex;align-items:center;gap:8px;flex:1;cursor:move}
.hdr-badge{width:26px;height:26px;border-radius:7px;flex-shrink:0;
  background:linear-gradient(165deg,#121828,#0A0F1A);
  border:1px solid rgba(45,212,192,.35);
  display:flex;align-items:center;justify-content:center}
.hdr-title{font-size:13px;font-weight:600;letter-spacing:.01em}
.status-pill{display:flex;align-items:center;gap:5px;padding:3px 9px 3px 7px;
  border-radius:999px;font-size:11px;font-weight:500;
  background:rgba(0,212,170,.1);color:var(--teal);white-space:nowrap}
.sdot{width:6px;height:6px;border-radius:50%;background:var(--teal);
  animation:spulse 2s ease-in-out infinite}
.status-pill.warn{background:rgba(245,165,36,.1);color:var(--amber)}
.status-pill.warn .sdot{background:var(--amber)}
.status-pill.crit{background:rgba(240,107,107,.1);color:var(--red)}
.status-pill.crit .sdot{background:var(--red);animation:none}
@keyframes spulse{0%,100%{opacity:1}50%{opacity:.3}}
.hdr-clock{font-family:var(--mono);font-size:11px;color:var(--fg2);
  font-variant-numeric:tabular-nums;white-space:nowrap}
.hdr-btn{width:28px;height:28px;border:0;border-radius:6px;background:transparent;
  color:var(--fg2);display:flex;align-items:center;justify-content:center;
  cursor:pointer;transition:color .15s,background .15s;flex-shrink:0}
.hdr-btn:hover{color:var(--red);background:rgba(240,107,107,.12)}

/* ── Scrollable content ── */
.content{padding:10px 12px;display:flex;flex-direction:column;gap:8px;
  overflow-y:auto;height:calc(100% - 44px)}
.content::-webkit-scrollbar{width:3px}
.content::-webkit-scrollbar-thumb{background:var(--border);border-radius:2px}

/* ── Section card ── */
.card{background:var(--card);border:1px solid var(--border2);border-radius:10px;padding:12px}

/* ── CPU ── */
.cpu-wrap{display:flex;gap:12px;align-items:center}
.ring-box{position:relative;width:76px;height:76px;flex-shrink:0}
.ring-box svg{display:block}
.ring-center{position:absolute;inset:0;display:flex;flex-direction:column;
  align-items:center;justify-content:center;gap:0}
.ring-num{font-family:var(--mono);font-size:19px;font-weight:500;
  font-variant-numeric:tabular-nums;line-height:1}
.ring-lbl{font-size:10px;color:var(--fg2);margin-top:2px;letter-spacing:.04em}
.cpu-right{flex:1;min-width:0;display:flex;flex-direction:column;gap:6px}
.row-hdr{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:2px}
.sec-title{font-size:12px;font-weight:700;letter-spacing:.02em}
.sec-sub{font-size:10px;color:var(--fg2)}
.spark{display:block;width:100%;overflow:visible}
.cpu-stats{display:grid;grid-template-columns:repeat(3,1fr);gap:4px;margin-top:2px}
.st-lbl{font-size:10px;color:var(--fg2)}
.st-val{font-family:var(--mono);font-size:11px;font-weight:500;
  font-variant-numeric:tabular-nums;margin-top:1px}

/* ── RAM / Disk ── */
.bar-row{display:flex;flex-direction:column;gap:5px}
.bar-row+.bar-row{margin-top:8px}
.bar-hdr{display:flex;align-items:baseline;gap:5px}
.bar-name{font-size:12px;font-weight:600;flex:1}
.bar-sub{font-family:var(--mono);font-size:10px;color:var(--fg2);
  font-variant-numeric:tabular-nums}
.bar-pct{font-family:var(--mono);font-size:12px;font-weight:600;
  font-variant-numeric:tabular-nums;min-width:36px;text-align:right}
.bar-track{height:5px;border-radius:3px;background:#161D28;overflow:hidden}
.bar-fill{height:100%;border-radius:3px;transition:width .7s ease,background .3s}

/* ── Network ── */
.net-row{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.net-card{background:var(--inner);border:1px solid var(--border2);border-radius:8px;
  padding:10px 12px;display:flex;align-items:center;gap:10px}
.net-icon{flex-shrink:0}
.net-right{flex:1;min-width:0}
.net-label{font-size:10px;color:var(--fg2);margin-bottom:3px}
.net-val{font-family:var(--mono);font-size:16px;font-weight:600;
  font-variant-numeric:tabular-nums;line-height:1}
.net-unit{font-size:10px;color:var(--fg2);margin-left:2px}

/* ── Processes ── */
.proc-list{display:flex;flex-direction:column;gap:6px}
.proc-row{display:flex;align-items:center;gap:8px}
.proc-name{flex:1;font-size:12px;white-space:nowrap;overflow:hidden;
  text-overflow:ellipsis;min-width:0}
.proc-cpu{font-family:var(--mono);font-size:11px;font-weight:600;
  font-variant-numeric:tabular-nums;min-width:38px;text-align:right}
.proc-mem{font-family:var(--mono);font-size:10px;color:var(--fg2);
  font-variant-numeric:tabular-nums;min-width:52px;text-align:right}
.proc-bar{width:56px;height:4px;border-radius:2px;background:#161D28;flex-shrink:0}
.proc-bar-fill{height:100%;border-radius:2px;background:var(--teal);
  transition:width .5s ease}
.no-procs{font-size:11px;color:var(--fg2);text-align:center;padding:6px 0}
</style>
</head>
<body>

<header class="hdr">
  <div class="hdr-drag" id="drag-handle">
    <div class="hdr-badge">
      <!-- Helix Orbit Pulse mark (matches app_icon.ico) -->
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none">
        <path d="M5.9 17.1 A 8.5 8.5 0 1 1 18.1 17.1"
              stroke="#2DD4C0" stroke-width="2.6" stroke-linecap="round"/>
        <polyline points="6,12 9.3,12 10.8,8.8 12.8,15.2 14.2,12 16.7,12"
              stroke="#8AF0FF" stroke-width="2" stroke-linecap="round"
              stroke-linejoin="round" fill="none"/>
        <circle cx="16.9" cy="12" r="1.35" fill="#FFFFFF"/>
      </svg>
    </div>
    <div class="hdr-title">Helix Monitor</div>
    <div class="status-pill" id="status-pill">
      <span class="sdot"></span>
      <span id="status-text">System healthy</span>
    </div>
  </div>
  <div class="hdr-clock" id="hud-clock">--:--:--</div>
</header>

<div class="content">

  <!-- CPU -->
  <div class="card">
    <div class="cpu-wrap">
      <div class="ring-box">
        <svg width="76" height="76" viewBox="0 0 76 76">
          <circle cx="38" cy="38" r="30" fill="none" stroke="#141C28" stroke-width="6"/>
          <circle id="cpu-ring" cx="38" cy="38" r="30" fill="none"
            stroke="#00D4AA" stroke-width="6" stroke-linecap="round"
            stroke-dasharray="0 188.5" transform="rotate(-90 38 38)"
            style="transition:stroke-dasharray .6s ease,stroke .3s"/>
        </svg>
        <div class="ring-center">
          <div class="ring-num" id="cpu-num" style="color:#00D4AA">—</div>
          <div class="ring-lbl">CPU %</div>
        </div>
      </div>
      <div class="cpu-right">
        <div class="row-hdr">
          <span class="sec-title">PROCESSOR</span>
          <span class="sec-sub">40 s history</span>
        </div>
        <svg class="spark" height="34" viewBox="0 0 200 34" preserveAspectRatio="none">
          <polygon id="cpu-area" points="0,34 200,34" fill="#00D4AA" fill-opacity="0.12"/>
          <polyline id="cpu-line" points="0,34 200,34" fill="none"
            stroke="#00D4AA" stroke-width="1.5" stroke-linejoin="round"
            vector-effect="non-scaling-stroke"/>
        </svg>
        <div class="cpu-stats">
          <div><div class="st-lbl">Clock</div><div class="st-val" id="cpu-ghz">—</div></div>
          <div><div class="st-lbl">Peak</div><div class="st-val" id="cpu-peak">—%</div></div>
          <div><div class="st-lbl">Uptime</div><div class="st-val" id="uptime">—</div></div>
        </div>
      </div>
    </div>
  </div>

  <!-- RAM + Disk -->
  <div class="card">
    <div class="bar-row">
      <div class="bar-hdr">
        <div class="bar-name">Memory</div>
        <div class="bar-sub" id="ram-used">— / — GB</div>
        <div class="bar-pct" id="ram-pct">—%</div>
      </div>
      <div class="bar-track"><div class="bar-fill" id="ram-fill" style="width:0%"></div></div>
    </div>
    <div class="bar-row">
      <div class="bar-hdr">
        <div class="bar-name">Disk C:</div>
        <div class="bar-sub" id="disk-used">— / — GB</div>
        <div class="bar-pct" id="disk-pct">—%</div>
      </div>
      <div class="bar-track"><div class="bar-fill" id="disk-fill" style="width:0%"></div></div>
    </div>
  </div>

  <!-- Network -->
  <div class="net-row">
    <div class="net-card">
      <div class="net-icon">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none"
          stroke="#F5A524" stroke-width="2" stroke-linecap="round">
          <path d="M12 19V5M5 12l7-7 7 7"/>
        </svg>
      </div>
      <div class="net-right">
        <div class="net-label">Upload</div>
        <div><span class="net-val" id="up-val" style="color:#F5A524">—</span><span class="net-unit" id="up-unit">KB/s</span></div>
      </div>
    </div>
    <div class="net-card">
      <div class="net-icon">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none"
          stroke="#5B9CF6" stroke-width="2" stroke-linecap="round">
          <path d="M12 5v14M5 12l7 7 7-7"/>
        </svg>
      </div>
      <div class="net-right">
        <div class="net-label">Download</div>
        <div><span class="net-val" id="dn-val" style="color:#5B9CF6">—</span><span class="net-unit" id="dn-unit">KB/s</span></div>
      </div>
    </div>
  </div>

  <!-- Top Processes -->
  <div class="card">
    <div class="row-hdr" style="margin-bottom:8px">
      <span class="sec-title">TOP PROCESSES</span>
      <span class="sec-sub">by CPU</span>
    </div>
    <div class="proc-list" id="proc-list">
      <div class="no-procs">Loading…</div>
    </div>
  </div>

</div>

<script>
const N=40;
const cpuH=new Array(N).fill(0);
let cpuPeak=0;
const CIRC=2*Math.PI*30;

function levelColor(v,w,c){return v>=c?'#F06B6B':v>=w?'#F5A524':'#00D4AA'}

function polyline(arr,h){
  const m=Math.max(...arr,1);
  return arr.map((v,i)=>{
    const x=(i/(N-1)*200).toFixed(1);
    const y=(h-1-(v/m)*(h-3)).toFixed(1);
    return x+','+y;
  }).join(' ');
}

function fmtNet(kb){
  if(kb>=1024)return{v:(kb/1024).toFixed(1),u:'MB/s'};
  return{v:Math.round(kb).toString(),u:'KB/s'};
}

function fmtUptime(s){
  const d=Math.floor(s/86400),h=Math.floor(s%86400/3600),m=Math.floor(s%3600/60);
  if(d>0)return d+'d '+h+'h '+String(m).padStart(2,'0')+'m';
  return h+'h '+String(m).padStart(2,'0')+'m';
}

function fmtMem(mb){
  if(mb>=1024)return(mb/1024).toFixed(1)+' GB';
  return Math.round(mb)+' MB';
}

// Clock
function tickClock(){
  const t=new Date();
  document.getElementById('hud-clock').textContent=
    [t.getHours(),t.getMinutes(),t.getSeconds()]
      .map(n=>String(n).padStart(2,'0')).join(':');
}
// Clock ticks only when the HUD window is visible — avoids running a 1Hz
// JS timer inside a hidden WebView2 process at all times.
let _clockTimer=null;
function _clockStart(){ if(_clockTimer) return; tickClock(); _clockTimer=setInterval(tickClock,1000); }
function _clockStop(){ if(_clockTimer){ clearInterval(_clockTimer); _clockTimer=null; } }
document.addEventListener('visibilitychange', ()=>{
  if(document.hidden) _clockStop(); else _clockStart();
});
if(!document.hidden) _clockStart();

// Drag — calls Python to trigger Win32 native drag
const dragHandle=document.getElementById('drag-handle');
dragHandle.addEventListener('mousedown',function(e){
  if(e.button!==0)return;
  try{window.pywebview.api.begin_drag();}catch(_){}
});

// Refresh
async function refresh(){
  let d;
  try{d=await window.pywebview.api.get_stats();}
  catch(e){return;}
  if(!d||d.error)return;

  const cpu=d.cpu??0,ramP=d.ram_pct??0,dkP=d.disk_pct??0;
  const upKb=d.net_up_kbs??0,dnKb=d.net_dn_kbs??0;

  cpuH.shift();cpuH.push(cpu);
  if(cpu>cpuPeak)cpuPeak=cpu;

  // Status pill
  const worst=Math.max(
    cpu>=90?2:cpu>=75?1:0,
    ramP>=90?2:ramP>=80?1:0,
    dkP>=95?2:dkP>=85?1:0
  );
  const pill=document.getElementById('status-pill');
  pill.className='status-pill'+(worst===2?' crit':worst===1?' warn':'');
  document.getElementById('status-text').textContent=
    worst===2?'Critical':worst===1?'High load':'Healthy';

  // CPU ring
  const cpuColor=levelColor(cpu,75,90);
  const ring=document.getElementById('cpu-ring');
  ring.setAttribute('stroke-dasharray',(CIRC*cpu/100).toFixed(1)+' '+CIRC.toFixed(1));
  ring.setAttribute('stroke',cpuColor);
  const cn=document.getElementById('cpu-num');
  cn.textContent=Math.round(cpu);cn.style.color=cpuColor;

  // Sparkline
  const cpuLine=polyline(cpuH,34);
  document.getElementById('cpu-line').setAttribute('points',cpuLine);
  document.getElementById('cpu-line').setAttribute('stroke',cpuColor);
  document.getElementById('cpu-area').setAttribute('points','0,34 '+cpuLine+' 200,34');
  document.getElementById('cpu-area').setAttribute('fill',cpuColor);

  document.getElementById('cpu-ghz').textContent=d.ghz?d.ghz.toFixed(2)+' GHz':'—';
  document.getElementById('cpu-peak').textContent=Math.round(cpuPeak)+'%';
  document.getElementById('uptime').textContent=d.uptime_s?fmtUptime(d.uptime_s):'—';

  // RAM
  const ramColor=levelColor(ramP,80,90);
  document.getElementById('ram-pct').textContent=Math.round(ramP)+'%';
  document.getElementById('ram-pct').style.color=ramColor;
  document.getElementById('ram-used').textContent=(d.ram_used_gb??'—')+' / '+(d.ram_total_gb??'—')+' GB';
  const rf=document.getElementById('ram-fill');
  rf.style.width=ramP+'%';rf.style.background=ramColor;

  // Disk
  const dkColor=levelColor(dkP,85,95);
  document.getElementById('disk-pct').textContent=Math.round(dkP)+'%';
  document.getElementById('disk-pct').style.color=dkColor;
  document.getElementById('disk-used').textContent=(d.disk_used_gb??'—')+' / '+(d.disk_total_gb??'—')+' GB';
  const df=document.getElementById('disk-fill');
  df.style.width=dkP+'%';df.style.background=dkColor;

  // Network
  const up=fmtNet(upKb),dn=fmtNet(dnKb);
  document.getElementById('up-val').textContent=up.v;
  document.getElementById('up-unit').textContent=up.u;
  document.getElementById('dn-val').textContent=dn.v;
  document.getElementById('dn-unit').textContent=dn.u;

  // Processes
  const procs=d.top_procs||[];
  const pl=document.getElementById('proc-list');
  if(!procs.length){
    pl.innerHTML='<div class="no-procs">No data yet…</div>';
  }else{
    const maxCpu=Math.max(...procs.map(p=>p.cpu),1);
    pl.innerHTML=procs.map(p=>{
      const pct=Math.round(p.cpu);
      const col=levelColor(p.cpu,40,70);
      const bar=Math.round(p.cpu/maxCpu*100);
      return `<div class="proc-row">
        <div class="proc-name" title="${p.name}">${p.name}</div>
        <div class="proc-bar"><div class="proc-bar-fill" style="width:${bar}%;background:${col}"></div></div>
        <div class="proc-cpu" style="color:${col}">${pct}%</div>
        <div class="proc-mem">${fmtMem(p.mem_mb)}</div>
      </div>`;
    }).join('');
  }
}

// Polling is started/stopped by Python via _hudPollStart() / _hudPollStop(),
// AND auto-stopped whenever the HUD window becomes hidden (pywebview's
// hide() does not fire a 'closing' event, so the Python hook alone is not
// enough — the hidden window would otherwise keep calling get_stats, which
// iterates every process on the system every second).
let _hudTimer=null;
function _hudPollStart(){
  if(document.hidden) return;
  if(_hudTimer) return;
  refresh();
  _hudTimer=setInterval(refresh,1000);
}
function _hudPollStop(){  if(_hudTimer){ clearInterval(_hudTimer); _hudTimer=null; } }
document.addEventListener('visibilitychange', ()=>{
  if(document.hidden) _hudPollStop(); else _hudPollStart();
});
</script>
</body>
</html>"""


# ── Python API ────────────────────────────────────────────────────────────────

class MonitorHudAPI:
    def __init__(self, main_api=None):
        self._main = main_api
        self._win  = None
        self._net_sent = 0
        self._net_recv = 0
        self._net_ts   = 0.0

    def set_window(self, w):
        self._win = w

    def get_stats(self) -> dict:
        try:
            import psutil
            cpu  = psutil.cpu_percent(interval=None)
            vm   = psutil.virtual_memory()
            du   = psutil.disk_usage("C:\\")
            now  = time.time()
            net  = psutil.net_io_counters()
            dt   = max(now - self._net_ts, 0.5) if self._net_ts else 1.0
            up   = max(0.0, (net.bytes_sent - self._net_sent) / dt / 1024)
            dn   = max(0.0, (net.bytes_recv - self._net_recv) / dt / 1024)
            self._net_sent = net.bytes_sent
            self._net_recv = net.bytes_recv
            self._net_ts   = now
            freq = psutil.cpu_freq()
            boot = psutil.boot_time()

            # Top processes by CPU
            procs = []
            for p in psutil.process_iter(['name', 'cpu_percent', 'memory_info']):
                try:
                    mi = p.info.get('memory_info')
                    procs.append({
                        'name':   (p.info.get('name') or 'unknown')[:22],
                        'cpu':    round(p.info.get('cpu_percent') or 0.0, 1),
                        'mem_mb': round(mi.rss / 1e6, 1) if mi else 0.0,
                    })
                except Exception:
                    pass
            top = sorted(procs, key=lambda x: x['cpu'], reverse=True)
            top = [p for p in top if p['cpu'] > 0][:4]

            return {
                "cpu":           round(cpu, 1),
                "ghz":           round(freq.current / 1000, 2) if freq else None,
                "ram_pct":       round(vm.percent, 1),
                "ram_used_gb":   round(vm.used  / 1e9, 1),
                "ram_total_gb":  round(vm.total / 1e9, 1),
                "disk_pct":      round(du.percent, 1),
                "disk_used_gb":  round(du.used  / 1e9, 1),
                "disk_total_gb": round(du.total / 1e9, 1),
                "net_up_kbs":    round(up, 1),
                "net_dn_kbs":    round(dn, 1),
                "uptime_s":      int(now - boot),
                "top_procs":     top,
            }
        except Exception as exc:
            return {"error": str(exc)}

    def begin_drag(self):
        """Trigger native Win32 window drag from the header."""
        try:
            import ctypes
            hwnd = ctypes.windll.user32.FindWindowW(None, HUD_TITLE)
            if hwnd:
                ctypes.windll.user32.ReleaseCapture()
                # WM_NCLBUTTONDOWN with HTCAPTION = 2 starts native move
                ctypes.windll.user32.PostMessageW(hwnd, 0x00A1, 2, 0)
        except Exception:
            pass

    def close_hud(self):
        if self._win:
            try: self._win.hide()
            except Exception: pass


# ── Win32 helpers ─────────────────────────────────────────────────────────────

def _set_topmost(title: str):
    try:
        import ctypes
        HWND_TOPMOST = -1
        SWP_NOSIZE   = 0x0001
        SWP_NOMOVE   = 0x0002
        hwnd = ctypes.windll.user32.FindWindowW(None, title)
        if hwnd:
            ctypes.windll.user32.SetWindowPos(
                hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOSIZE | SWP_NOMOVE
            )
    except Exception:
        pass


def _position_top_right(title: str, width: int, height: int, margin: int = 16):
    try:
        import ctypes
        user32 = ctypes.windll.user32
        sw = user32.GetSystemMetrics(0)
        x  = sw - width - margin
        hwnd = user32.FindWindowW(None, title)
        if hwnd:
            user32.SetWindowPos(hwnd, 0, x, margin, width, height, 0x0004)
    except Exception:
        pass


def start_hud_system(main_api, hud_window):
    global _hud_api, _hud_win
    _hud_win = hud_window
    _hud_api = MonitorHudAPI(main_api)
    _hud_api.set_window(hud_window)

    def on_closing():
        try: hud_window.hide()
        except Exception: pass
        return False
    hud_window.events.closing += on_closing


def open_hud():
    if _hud_win:
        try: _hud_win.show()
        except Exception: pass
        def _pin():
            time.sleep(0.25)
            _set_topmost(HUD_TITLE)
        threading.Thread(target=_pin, daemon=True).start()


def close_hud():
    if _hud_win:
        try: _hud_win.hide()
        except Exception: pass


def toggle_hud():
    if _hud_win:
        try:
            import ctypes
            hwnd = ctypes.windll.user32.FindWindowW(None, HUD_TITLE)
            if hwnd and ctypes.windll.user32.IsWindowVisible(hwnd):
                close_hud()
                return
        except Exception:
            pass
        open_hud()


def get_hud_api():
    return _hud_api
