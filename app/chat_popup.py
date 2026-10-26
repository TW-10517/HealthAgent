"""PC Health Agent — Chat popup system.

The chat popup is opened via the orbital launcher's Chat satellite.
No separate floating button — everything is accessed through the launcher icon.

Provides:
  ChatPopupAPI    — pywebview JS API for the popup window
  CHAT_POPUP_HTML — standalone HTML for the compact chat window
  start_chat_system / stop_chat_system / open_popup / close_popup
"""
import threading
import db

_ENABLED_KEY = "chat_float_enabled"

_popup_api  = None
_popup_win  = None
_lock = threading.Lock()


def is_float_enabled():
    return db.get_setting(_ENABLED_KEY) != "0"


def set_float_enabled(enabled: bool):
    db.set_setting(_ENABLED_KEY, "1" if enabled else "0")


# ── pywebview chat popup API ──────────────────────────────────────────────────

class ChatPopupAPI:
    """Thin JS API for the chat popup window. Delegates to AgentAPI.chat()."""

    def __init__(self, main_api):
        self._main = main_api
        self._popup_window = None

    def set_popup_window(self, w):
        self._popup_window = w

    def chat(self, message: str, lang: str = "en") -> dict:
        return self._main.chat(message, lang, _popup_window=self._popup_window)

    def get_popup_settings(self) -> dict:
        try:
            lang = db.get_setting("language") or "en"
        except Exception:
            lang = "en"
        return {"lang": lang}

    def navigate_main(self, page: str):
        """Open main window and navigate to a page (called from popup JS)."""
        w = self._main._window
        if w:
            try:
                w.show()
                w.restore()
                w.evaluate_js(f"showPage('{page}')")
            except Exception:
                pass

    def close_popup(self):
        if self._popup_window:
            try: self._popup_window.hide()
            except Exception: pass


# ── Chat popup HTML ────────────────────────────────────────────────────────────

CHAT_POPUP_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Helix Assistant</title>
<style>
:root{
  --bg:#07090F;--bg2:#0D1220;--bg3:#131C30;
  --brand:#00D4AA;--brand-dk:#007A62;
  --text:#DDE4F0;--muted:#5A6880;
  --border:#1A2540;--r:14px;
}
*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%;overflow:hidden}
body{
  background:var(--bg);color:var(--text);
  font-family:'Segoe UI',system-ui,sans-serif;font-size:13px;
  display:flex;flex-direction:column;
}
.hdr{
  display:flex;align-items:center;gap:10px;
  padding:12px 16px;flex-shrink:0;
  background:linear-gradient(180deg,#0F1828 0%,var(--bg2) 100%);
  border-bottom:1px solid var(--border);user-select:none;
}
.hdr-badge{
  width:32px;height:32px;border-radius:9px;flex-shrink:0;
  background:linear-gradient(165deg,#121828,#0A0F1A);
  border:1px solid rgba(45,212,192,.35);
  display:flex;align-items:center;justify-content:center;
  box-shadow:0 2px 8px #00D4AA30;
}
.hdr-info{flex:1}
.hdr-name{font-weight:700;font-size:13px;letter-spacing:.01em}
.hdr-status{font-size:10px;color:var(--muted);margin-top:2px;display:flex;align-items:center;gap:5px}
.dot-live{width:6px;height:6px;border-radius:50%;background:#00D4AA;display:inline-block;
  box-shadow:0 0 4px #00D4AA80;animation:pulse 2s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
#msgs{
  flex:1;overflow-y:auto;padding:16px 14px 8px;
  display:flex;flex-direction:column;gap:10px;
}
#msgs::-webkit-scrollbar{width:3px}
#msgs::-webkit-scrollbar-thumb{background:#1E2D44;border-radius:2px}
.msg{
  max-width:86%;padding:9px 13px;line-height:1.55;
  word-wrap:break-word;font-size:13px;
  animation:popIn .16s ease;
}
@keyframes popIn{from{opacity:0;transform:translateY(5px)}to{opacity:1;transform:none}}
.msg.user{
  align-self:flex-end;
  background:linear-gradient(135deg,#005C4B,#00B894);
  color:#fff;border-radius:var(--r) var(--r) 3px var(--r);
}
.msg.bot{
  align-self:flex-start;
  background:var(--bg3);border:1px solid var(--border);
  color:var(--text);border-radius:var(--r) var(--r) var(--r) 3px;
}
.msg.err{
  align-self:flex-start;
  background:#160808;border:1px solid #E0404040;
  color:#E08080;border-radius:var(--r) var(--r) var(--r) 3px;
}
.msg.thinking{
  align-self:flex-start;
  background:var(--bg3);border:1px solid var(--border);
  border-radius:var(--r) var(--r) var(--r) 3px;
  color:var(--muted);font-style:italic;padding:8px 14px;
}
.msg strong{font-weight:600;color:#5DDFC0}
@keyframes blink{0%,80%{opacity:1}40%{opacity:.15}}
.dot-anim span{animation:blink 1.2s infinite both}
.dot-anim span:nth-child(2){animation-delay:.2s}
.dot-anim span:nth-child(3){animation-delay:.4s}
.ibar{
  padding:10px 12px 12px;background:var(--bg2);
  border-top:1px solid var(--border);flex-shrink:0;
  display:flex;align-items:center;gap:8px;
}
#inp{
  flex:1;height:38px;
  background:var(--bg3);border:1px solid var(--border);
  border-radius:10px;color:var(--text);
  font-family:inherit;font-size:13px;
  padding:0 13px;outline:none;
  transition:border-color .15s;
}
#inp:focus{border-color:var(--brand)}
#inp::placeholder{color:var(--muted)}
#inp:disabled{opacity:.5}
#send{
  width:38px;height:38px;border-radius:10px;border:none;cursor:pointer;
  background:var(--brand);color:#041410;
  display:flex;align-items:center;justify-content:center;flex-shrink:0;
  transition:background .15s,transform .1s;
}
#send:hover{background:#1FFFD6;transform:scale(1.05)}
#send:active{transform:scale(.96)}
#send:disabled{opacity:.25;cursor:default;transform:none}
</style>
</head>
<body>
<div class="hdr">
  <div class="hdr-badge">
    <!-- Helix Orbit Pulse mark (matches app_icon.ico) -->
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none">
      <path d="M5.9 17.1 A 8.5 8.5 0 1 1 18.1 17.1" stroke="#2DD4C0" stroke-width="2.6" stroke-linecap="round"/>
      <polyline points="6,12 9.3,12 10.8,8.8 12.8,15.2 14.2,12 16.7,12" stroke="#8AF0FF" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" fill="none"/>
      <circle cx="16.9" cy="12" r="1.35" fill="#FFFFFF"/>
    </svg>
  </div>
  <div class="hdr-info">
    <div class="hdr-name" id="tb-title">Helix Assistant</div>
    <div class="hdr-status"><span class="dot-live"></span><span id="tb-sub">Local AI · private</span></div>
  </div>
</div>
<div id="msgs"></div>
<div class="ibar">
  <input type="text" id="inp" autocomplete="off" spellcheck="false">
  <button id="send" title="Send">
    <svg width="15" height="15" viewBox="0 0 24 24" fill="currentColor">
      <path d="M2.01 21L23 12 2.01 3 2 10l15 2-15 2z"/>
    </svg>
  </button>
</div>
<script>
const msgsEl=document.getElementById('msgs');
const inp=document.getElementById('inp');
const sendBtn=document.getElementById('send');
let _busy=false,_lang='en';

const _T={
  en:{
    title:'Helix Assistant',sub:'Local AI · private',
    welcome:'Hi! Ask me about disk space, RAM, running apps, startup items, or anything about your PC health.',
    ph:'Type a question…',thinking:'Thinking',err:'Connection error — is the app running?',
  },
  ja:{
    title:'PCヘルスアシスタント',sub:'ローカルAI · プライベート',
    welcome:'PCヘルスアシスタントです。ディスク容量・RAM・実行中アプリ・起動項目など、何でもお聞きください。',
    ph:'質問を入力…',thinking:'考えています',err:'接続エラー。アプリが起動していますか？',
  }
};
function t(k){return(_T[_lang]||_T.en)[k]||_T.en[k]}

function applyLang(){
  document.getElementById('tb-title').textContent=t('title');
  document.getElementById('tb-sub').textContent=t('sub');
  inp.placeholder=t('ph');
}

function esc(s){
  return String(s)
    .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/\*\*(.*?)\*\*/g,'<strong>$1</strong>')
    .replace(/\n/g,'<br>');
}

function addMsg(text,cls){
  const el=document.createElement('div');
  el.className='msg '+cls;
  el.innerHTML=esc(text);
  msgsEl.appendChild(el);
  msgsEl.scrollTop=msgsEl.scrollHeight;
  return el;
}

function setThinking(on){
  _busy=on;
  sendBtn.disabled=on;
  inp.disabled=on;
  if(on){
    const el=document.createElement('div');
    el.className='msg thinking dot-anim';el.id='think';
    el.innerHTML=t('thinking')+' <span>.</span><span>.</span><span>.</span>';
    msgsEl.appendChild(el);
    msgsEl.scrollTop=msgsEl.scrollHeight;
  }else{
    const el=document.getElementById('think');
    if(el)el.remove();
    inp.disabled=false;
    setTimeout(()=>inp.focus(),0);
  }
}

async function send(){
  const text=inp.value.trim();
  if(!text||_busy)return;
  addMsg(text,'user');
  inp.value='';
  setThinking(true);
  try{
    const r=await window.pywebview.api.chat(text,_lang);
    if(r&&r.type==='thinking')return;
    setThinking(false);
    handleResp(r);
  }catch(e){
    setThinking(false);
    addMsg(t('err'),'err');
  }
}

function handleResp(r){
  if(!r)return;
  const cls='bot'+(r.type==='error'?' err':'');
  if(r.type==='action'&&r.action==='navigate'){
    addMsg(r.content,cls);
    try{window.pywebview.api.navigate_main(r.page);}catch(e){}
    return;
  }
  if(r.content)addMsg(r.content,cls);
}

function onChatResponse(r){setThinking(false);handleResp(r);}

inp.addEventListener('keydown',function(e){
  if(e.key==='Enter'){e.preventDefault();e.stopPropagation();send();}
});
sendBtn.addEventListener('click',send);

async function init(){
  try{
    const s=await window.pywebview.api.get_popup_settings();
    if(s&&s.lang)_lang=s.lang;
  }catch(e){}
  applyLang();
  if(!msgsEl.children.length)addMsg(t('welcome'),'bot');
  inp.focus();
}

window.addEventListener('pywebviewready',init);
if(window.pywebview)init();else{applyLang();addMsg(t('welcome'),'bot');}
</script>
</body>
</html>"""


# ── Module-level lifecycle ────────────────────────────────────────────────────

def start_chat_system(main_api, popup_window):
    """Call once from run() after creating the popup window."""
    global _popup_api, _popup_win

    _popup_win = popup_window
    _popup_api = ChatPopupAPI(main_api)
    _popup_api.set_popup_window(popup_window)

    def on_popup_closing():
        try: popup_window.hide()
        except Exception: pass
        return False
    popup_window.events.closing += on_popup_closing


def stop_chat_system():
    pass  # nothing to clean up — popup window is managed by pywebview


def open_popup():
    if _popup_win:
        try: _popup_win.show()
        except Exception: pass


def close_popup():
    if _popup_win:
        try: _popup_win.hide()
        except Exception: pass


def set_float_button_enabled(enabled: bool):
    set_float_enabled(enabled)


def get_popup_api():
    return _popup_api
