"""Generates the bilingual HTML report with language toggle."""
from scanner import fmt_size

# ── Palette / CSS (matches the morning PC Health Report artifact) ─────────────
CSS = """
:root{
  --bg:#f6f5f2;--surface:#fff;--surface2:#f0efe9;--border:#e4e2dc;
  --fg:#181715;--fg2:#6b6560;--fg3:#b0ada6;
  --accent:#1d6bde;--accent-bg:#eef4ff;--accent-border:#c3d9ff;
  --green:#15803d;--green-bg:#f0fdf4;--green-chip:#bbf7d0;--green-border:#86efac;
  --yellow:#92400e;--yellow-bg:#fffbeb;--yellow-chip:#fde68a;--yellow-border:#fcd34d;
  --red:#b91c1c;--red-bg:#fff1f1;--red-chip:#fecaca;--red-border:#fca5a5;
  --teal:#0e7490;--teal-bg:#f0fdfa;--teal-border:#5eead4;
  --mono:'Consolas','Courier New',monospace;
}
@media(prefers-color-scheme:dark){
  :root{
    --bg:#111110;--surface:#1c1b19;--surface2:#232220;--border:#2e2d2a;
    --fg:#f5f4f0;--fg2:#9e9890;--fg3:#5a5650;
    --accent:#5b9bff;--accent-bg:#0d1f3c;--accent-border:#1e3d6e;
    --green:#4ade80;--green-bg:#052e16;--green-chip:#14532d;--green-border:#166534;
    --yellow:#fbbf24;--yellow-bg:#1c1003;--yellow-chip:#451a03;--yellow-border:#78350f;
    --red:#f87171;--red-bg:#1f0505;--red-chip:#450a0a;--red-border:#7f1d1d;
    --teal:#2dd4bf;--teal-bg:#042f2e;--teal-border:#115e59;
    color-scheme:dark;
  }
}
*,*::before,*::after{box-sizing:border-box}
body{font-family:-apple-system,'Segoe UI',system-ui,sans-serif;background:var(--bg);
  color:var(--fg);margin:0;padding:24px 20px;padding-block:32px;font-size:14px;
  line-height:1.6;max-width:980px;margin-inline:auto}
h2,h3{text-wrap:balance;margin:0}
code{font-family:var(--mono);font-size:11px}
a{color:var(--accent)}

/* Language toggle */
[data-lang="en"] .ja { display:none !important }
[data-lang="ja"] .en { display:none !important }
.lang-toggle{display:inline-flex;border:1px solid var(--border);border-radius:100px;overflow:hidden;background:var(--surface2)}
.lang-toggle button{border:none;background:transparent;padding:5px 14px;font-size:12px;font-weight:600;
  color:var(--fg2);cursor:pointer;transition:all .15s}
.lang-toggle button.active{background:var(--accent);color:#fff}
.lang-toggle button:hover:not(.active){background:var(--surface);color:var(--fg)}

/* Header */
.header{display:flex;align-items:center;justify-content:space-between;
  padding:16px 20px;background:var(--surface);border:1px solid var(--border);
  border-radius:12px;margin-bottom:20px;flex-wrap:wrap;gap:12px}
.header-left h2{font-size:1.1rem;font-weight:700}
.header-left .sub{font-size:12px;color:var(--fg3);margin-top:2px}
.badge{font-size:11px;font-weight:700;padding:4px 12px;border-radius:100px;
  background:var(--accent-bg);border:1px solid var(--accent-border);color:var(--accent)}
.header-right{display:flex;align-items:center;gap:10px}

/* Stat tiles */
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(145px,1fr));gap:8px;margin-bottom:28px}
.stat{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:14px 16px}
.stat .lbl{font-size:10px;color:var(--fg3);text-transform:uppercase;letter-spacing:.05em;margin-bottom:4px}
.stat .val{font-size:1.4rem;font-weight:700;font-variant-numeric:tabular-nums;font-family:var(--mono);line-height:1.2}
.stat .sub{font-size:11px;color:var(--fg3);margin-top:3px}

/* Section */
.sec{margin-bottom:36px}
.sec-title{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.07em;
  color:var(--fg3);margin-bottom:14px;padding-bottom:8px;border-bottom:1px solid var(--border);
  display:flex;align-items:center;gap:8px}

/* Perf cards */
.perf-grid{display:flex;flex-direction:column;gap:8px}
.perf-card{display:grid;grid-template-columns:36px 1fr;gap:0;border-radius:10px;overflow:hidden;border:1px solid}
.perf-card.red{border-color:var(--red-border);background:var(--red-bg)}
.perf-card.yellow{border-color:var(--yellow-border);background:var(--yellow-bg)}
.perf-card.green{border-color:var(--green-border);background:var(--green-bg)}
.perf-card-icon{display:flex;align-items:flex-start;justify-content:center;padding:14px 0 0;font-size:18px}
.perf-card-body{padding:12px 16px 12px 4px}
.perf-card-body .title-en{font-size:13px;font-weight:600}
.perf-card-body .title-ja{font-size:12px;color:var(--fg3);margin-bottom:6px}
.perf-card-body .detail{font-size:12px;color:var(--fg2);line-height:1.5}
.perf-card-body .fix{font-size:12px;font-weight:600;margin-top:5px}

/* Tables */
.tbl-wrap{overflow-x:auto}
table{width:100%;border-collapse:collapse;background:var(--surface);
  border:1px solid var(--border);border-radius:10px;overflow:hidden;font-size:13px}
thead th{background:var(--surface2);font-size:10px;text-transform:uppercase;
  letter-spacing:.05em;font-weight:600;color:var(--fg3);padding:9px 14px;
  text-align:left;border-bottom:1px solid var(--border)}
tbody tr{border-bottom:1px solid var(--border)}
tbody tr:last-child{border-bottom:none}
tbody td{padding:9px 14px;vertical-align:middle}
tbody tr:hover{background:var(--surface2)}

/* RAM bar */
.ram-bar-wrap{display:flex;align-items:center;gap:10px}
.ram-bar-track{flex:1;background:var(--surface2);border-radius:4px;height:7px;min-width:80px}
.ram-bar-fill{height:7px;border-radius:4px}
.ram-val{font-weight:700;min-width:64px;text-align:right;font-family:var(--mono);font-size:12px}

/* Chips */
.chip{display:inline-block;font-size:10px;font-weight:700;padding:1px 8px;
  border-radius:100px;vertical-align:middle;margin-left:5px}
.chip-red{background:var(--red-chip);color:var(--red)}
.chip-yellow{background:var(--yellow-chip);color:var(--yellow)}
.chip-green{background:var(--green-chip);color:var(--green)}

/* Note boxes */
.note{font-size:12px;color:var(--fg2);margin-top:8px;padding:10px 14px;border-radius:7px;border:1px solid}
.note-green{background:var(--green-bg);border-color:var(--green-border)}
.note-yellow{background:var(--yellow-bg);border-color:var(--yellow-border)}
.note-teal{background:var(--teal-bg);border-color:var(--teal-border)}

/* Footer */
.footer{font-size:11px;color:var(--fg3);text-align:center;
  margin-top:40px;padding-top:16px;border-top:1px solid var(--border)}

@media(max-width:600px){
  body{padding:16px 14px}
  .stats{grid-template-columns:1fr 1fr}
}
"""

JS = """
const LANGS = ['both','en','ja'];
const LABELS = {both:'EN / 日本語', en:'EN', ja:'日本語'};
let current = 0;

function setLang(idx) {
  current = idx;
  const lang = LANGS[idx];
  document.documentElement.setAttribute('data-lang', lang);
  LANGS.forEach((l, i) => {
    const btn = document.getElementById('btn-' + l);
    if (btn) btn.classList.toggle('active', i === idx);
  });
  try { localStorage.setItem('pcha-lang', lang) } catch(e) {}
}

document.addEventListener('DOMContentLoaded', () => {
  let saved = 'both';
  try { saved = localStorage.getItem('pcha-lang') || 'both' } catch(e) {}
  const idx = LANGS.indexOf(saved);
  setLang(idx >= 0 ? idx : 0);
});
"""

# ── Bilingual text helper ─────────────────────────────────────────────────────
def bi(en: str, ja: str) -> str:
    return f'<span class="en">{en}</span><span class="ja">{ja}</span>'

def bi_block(en: str, ja: str) -> str:
    return f'<span class="en">{en}</span><span class="ja" style="display:block">{ja}</span>'

# ── Performance analysis ───────────────────────────────────────────────────────
def build_perf_cards(data: dict) -> str:
    cards = []
    disk     = data["disk"]
    startup  = data["startup"]
    personal = data["personal_apps"]
    procs    = data["processes"]
    total_temp = data["total_temp"]

    if len(startup) > 8:
        cards.append(("yellow", "🚀",
            "Many startup apps slow boot time",
            "起動アプリが多くブート時間が遅い",
            f"{len(startup)} apps launch with Windows. {len(personal)} are personal apps you can review.",
            f"Windowsと同時に{len(startup)}個が起動。{len(personal)}個は個人アプリで見直し可能。",
            "Task Manager → Startup apps → disable what you don't need.",
            "タスクマネージャー → スタートアップ → 不要なものを無効化。",
        ))

    browser = next((p for p in procs if any(x in p["name"].lower() for x in ["msedge","chrome","firefox"])), None)
    if browser and browser["mem_mb"] > 800:
        cards.append(("yellow", "🌐",
            f"{browser['name']} is using {browser['mem_mb']} MB of RAM",
            f"{browser['name']}が {browser['mem_mb']} MBのRAMを使用中",
            "Close unused tabs or enable Sleeping Tabs to reduce browser RAM usage.",
            "未使用タブを閉じるか、スリーピングタブを有効にしてRAM使用量を削減。",
            "Edge: Settings → System → Sleeping Tabs → Enable",
            "Edge設定 → システム → スリーピングタブ → 有効にする",
        ))

    if disk["pct_free"] < 20:
        cards.append(("red", "💾",
            f"Disk critically low — {disk['pct_free']}% free ({disk['fmt_free']})",
            f"ディスク残量が危機的 — 空き{disk['pct_free']}% ({disk['fmt_free']})",
            "Windows needs at least 15% free disk for updates, swap and temp operations.",
            "Windowsはアップデート・スワップ・一時操作のために最低15%の空きが必要。",
            "Clean temp files and duplicates in this report.",
            "下の一時ファイルと重複ファイルを削除して容量を確保してください。",
        ))

    if total_temp > 500 * (1 << 20):
        cards.append(("yellow", "🗑️",
            f"{fmt_size(total_temp)} in temp and cache files",
            f"{fmt_size(total_temp)}の一時・キャッシュファイルが蓄積",
            "Accumulated temp files waste disk space and slow file operations.",
            "溜まった一時ファイルはディスクを無駄に消費し、動作を遅くします。",
            "All temp items below are safe to delete.",
            "下の一時ファイルはすべて安全に削除できます。",
        ))

    if not cards:
        cards.append(("green", "✅",
            "No significant performance issues found",
            "パフォーマンス上の重大な問題は見つかりませんでした",
            "Your PC is running well. The agent will notify you when issues arise.",
            "PCは良好な状態で動作しています。問題が生じたときに通知します。",
            "", "",
        ))

    html = ""
    for level, icon, en_t, ja_t, en_d, ja_d, en_f, ja_f in cards:
        fix_html = ""
        if en_f:
            fix_html = (
                f'<div class="fix en">→ {en_f}</div>'
                f'<div class="fix ja" style="color:var(--fg3)">→ {ja_f}</div>'
            )
        html += f"""<div class="perf-card {level}">
  <div class="perf-card-icon">{icon}</div>
  <div class="perf-card-body">
    <div class="title-en">{en_t}</div>
    <div class="title-ja">{ja_t}</div>
    <div class="detail">
      <span class="en">{en_d}</span>
      <span class="ja">{ja_d}</span>
    </div>
    {fix_html}
  </div>
</div>\n"""
    return html

# ── Section builders ───────────────────────────────────────────────────────────
def build_temp_rows(items):
    rows = ""
    for t in items:
        if t["size"] > 500*(1<<20):   col, ico = "var(--red)",    "🔴"
        elif t["size"] > 100*(1<<20): col, ico = "var(--yellow)", "🟡"
        else:                          col, ico = "var(--green)",  "🟢"
        rows += (
            f'<tr><td>{ico} <span class="en">{t["name_en"]}</span>'
            f'<span class="ja" style="display:block;font-size:11px;color:var(--fg3)">{t["name_ja"]}</span></td>'
            f'<td><code style="word-break:break-all">{t["path"]}</code></td>'
            f'<td style="font-weight:700;color:{col};white-space:nowrap">{t["size_fmt"]}</td></tr>\n'
        )
    return rows

def build_dupe_rows(groups):
    rows = ""
    for g in groups[:20]:
        names = ", ".join(f["name"] for f in g["files"][:2])
        if g["count"] > 2: names += f" +{g['count']-2} more"
        rows += (
            f'<tr><td style="word-break:break-word">{names}</td>'
            f'<td style="text-align:center">{g["count"]}</td>'
            f'<td style="white-space:nowrap">{g["total_fmt"]}</td>'
            f'<td style="color:var(--green);font-weight:700;white-space:nowrap">{g["saveable_fmt"]}</td></tr>\n'
        )
    return rows

def build_startup_rows(apps):
    rows = ""
    for a in sorted(apps, key=lambda x: not x["it_managed"]):
        if a["it_managed"]:
            chip   = '<span class="chip chip-red">IT管理 / IT-managed</span>'
            action = '<span style="color:var(--fg3);font-size:12px">Skip / スキップ</span>'
        else:
            chip   = '<span class="chip chip-yellow">個人 / Personal</span>'
            action = '<span style="color:var(--yellow);font-size:12px;font-weight:600">Review / 確認</span>'
        cmd = (a["command"][:65] + "…") if len(a["command"]) > 65 else a["command"]
        rows += (
            f'<tr><td>{a["name"]} {chip}</td>'
            f'<td style="font-size:11px;color:var(--fg3);max-width:320px;overflow:hidden;'
            f'text-overflow:ellipsis;white-space:nowrap"><code title="{a["command"]}">{cmd}</code></td>'
            f'<td>{action}</td></tr>\n'
        )
    return rows

def build_proc_rows(procs, total_ram_gb):
    rows = ""
    cap = (total_ram_gb or 32) * 1024
    for p in procs:
        pct = min(round(p["mem_mb"] / cap * 100), 100)
        col = "var(--red)" if p["mem_mb"] > 1500 else "var(--yellow)" if p["mem_mb"] > 600 else "var(--green)"
        bar = (
            f'<div class="ram-bar-wrap">'
            f'<div class="ram-bar-track"><div class="ram-bar-fill" style="width:{pct}%;background:{col}"></div></div>'
            f'<span class="ram-val" style="color:{col}">{p["mem_mb"]} MB</span></div>'
        )
        rows += f'<tr><td>{p["name"]}</td><td>{bar}</td></tr>\n'
    return rows

# ── Full report HTML ───────────────────────────────────────────────────────────
def generate(data: dict) -> str:
    disk  = data["disk"]
    temp  = data["temp_items"]
    dupes = data["dupes"]
    procs = data["processes"]
    start = data["startup"]
    ram   = data["total_ram_gb"]

    disk_col  = "var(--red)" if disk["pct_free"]<15 else "var(--yellow)" if disk["pct_free"]<25 else "var(--green)"
    dupe_col  = "var(--yellow)" if data["total_dupes"] > 100*(1<<20) else "var(--fg3)"
    start_col = "var(--yellow)" if len(start) > 8 else "var(--green)"
    top_name  = procs[0]["name"] if procs else "n/a"
    top_mb    = procs[0]["mem_mb"] if procs else 0

    perf_html    = build_perf_cards(data)
    temp_rows    = build_temp_rows(temp)
    dupe_rows    = build_dupe_rows(dupes)
    startup_rows = build_startup_rows(start)
    proc_rows    = build_proc_rows(procs, ram)

    if data.get("dupes_skipped"):
        dupe_body = '<p class="note note-yellow">Duplicate scan was skipped. Run without --no-dupes to enable.<br>重複スキャンはスキップされました。--no-dupesなしで再実行すると有効になります。</p>'
    elif not dupes:
        dupe_body = '<p style="color:var(--fg3);font-size:13px;padding:8px 0">No duplicates found. / 重複ファイルは見つかりませんでした。</p>'
    else:
        dupe_body = (
            f'<p style="font-size:13px;color:var(--fg2);margin-bottom:12px">'
            f'<span class="en">Found <strong>{data["dupe_groups_count"]} duplicate groups</strong> — '
            f'<strong style="color:var(--green)">{data["total_dupes_fmt"]} saveable</strong></span>'
            f'<span class="ja">重複グループ<strong>{data["dupe_groups_count"]}件</strong>を検出 — '
            f'<strong style="color:var(--green)">{data["total_dupes_fmt"]}節約可能</strong></span></p>'
            f'<div class="tbl-wrap"><table>'
            f'<thead><tr>'
            f'<th><span class="en">Files</span><span class="ja">ファイル</span></th>'
            f'<th><span class="en">Copies</span><span class="ja">コピー数</span></th>'
            f'<th><span class="en">Total</span><span class="ja">合計</span></th>'
            f'<th><span class="en">Saveable</span><span class="ja">節約可能</span></th>'
            f'</tr></thead><tbody>{dupe_rows}</tbody></table></div>'
        )

    return f"""<!DOCTYPE html>
<html lang="ja" data-lang="both">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>PC Health Report — {data['generated_at']}</title>
<style>{CSS}</style>
</head>
<body>

<div class="header">
  <div class="header-left">
    <h2>🏥 PC Health Report</h2>
    <div class="sub">{data['computer']} &nbsp;·&nbsp; {data['user']} &nbsp;·&nbsp; {data['generated_at']}</div>
  </div>
  <div class="header-right">
    <div class="lang-toggle">
      <button id="btn-both"  class="active" onclick="setLang(0)">EN / 日本語</button>
      <button id="btn-en"                   onclick="setLang(1)">EN</button>
      <button id="btn-ja"                   onclick="setLang(2)">日本語</button>
    </div>
    <span class="badge">v0.1 Demo</span>
  </div>
</div>

<div class="stats">
  <div class="stat">
    <div class="lbl"><span class="en">Free space</span><span class="ja">空き容量</span></div>
    <div class="val" style="color:{disk_col}">{disk['fmt_free']}</div>
    <div class="sub">{disk['pct_free']}% of {disk['total_gb']} GB</div>
  </div>
  <div class="stat">
    <div class="lbl"><span class="en">Reclaimable</span><span class="ja">解放可能</span></div>
    <div class="val" style="color:var(--yellow)">{data['total_freeable_fmt']}</div>
    <div class="sub"><span class="en">temp + duplicates</span><span class="ja">一時+重複</span></div>
  </div>
  <div class="stat">
    <div class="lbl"><span class="en">Temp files</span><span class="ja">一時ファイル</span></div>
    <div class="val" style="color:var(--yellow)">{data['total_temp_fmt']}</div>
    <div class="sub">{len(temp)} <span class="en">locations</span><span class="ja">か所</span></div>
  </div>
  <div class="stat">
    <div class="lbl"><span class="en">Duplicates</span><span class="ja">重複ファイル</span></div>
    <div class="val" style="color:{dupe_col}">{data['total_dupes_fmt']}</div>
    <div class="sub">{data['dupe_groups_count']} <span class="en">groups</span><span class="ja">グループ</span></div>
  </div>
  <div class="stat">
    <div class="lbl"><span class="en">Startup apps</span><span class="ja">起動アプリ</span></div>
    <div class="val" style="color:{start_col}">{len(start)}</div>
    <div class="sub">{len(data['personal_apps'])} <span class="en">personal</span><span class="ja">個人</span></div>
  </div>
  <div class="stat">
    <div class="lbl"><span class="en">Top process</span><span class="ja">最大プロセス</span></div>
    <div class="val">{top_mb} <span style="font-size:.7em">MB</span></div>
    <div class="sub">{top_name}</div>
  </div>
</div>

<div class="sec">
  <div class="sec-title">⚡ <span class="en">Why your PC may be slow — Performance analysis</span><span class="ja">パフォーマンス分析 — PCが遅い原因</span></div>
  <div class="perf-grid">{perf_html}</div>
</div>

<div class="sec">
  <div class="sec-title">🗑️ <span class="en">Temp &amp; cache — {data['total_temp_fmt']}</span><span class="ja">一時・キャッシュ — {data['total_temp_fmt']}</span></div>
  <div class="tbl-wrap"><table>
    <thead><tr>
      <th><span class="en">Location</span><span class="ja">場所</span></th>
      <th>Path</th>
      <th><span class="en">Size</span><span class="ja">サイズ</span></th>
    </tr></thead>
    <tbody>{temp_rows}</tbody>
  </table></div>
  <div class="note note-green">✅ <span class="en">All items above are safe to delete — caches rebuild automatically.</span><span class="ja">上記はすべて安全に削除できます。キャッシュは自動で再生成されます。</span></div>
</div>

<div class="sec">
  <div class="sec-title">👯 <span class="en">Duplicate files (Downloads)</span><span class="ja">重複ファイル（Downloadsフォルダ）</span></div>
  {dupe_body}
</div>

<div class="sec">
  <div class="sec-title">🚀 <span class="en">Startup apps — {len(start)} total</span><span class="ja">起動アプリ一覧 — {len(start)}個</span></div>
  <div class="tbl-wrap"><table>
    <thead><tr>
      <th><span class="en">App</span><span class="ja">アプリ</span></th>
      <th><span class="en">Command</span><span class="ja">コマンド</span></th>
      <th><span class="en">Action</span><span class="ja">対応</span></th>
    </tr></thead>
    <tbody>{startup_rows}</tbody>
  </table></div>
  <div class="note note-teal">
    <span class="en">Red = IT-managed (never disable). Yellow = personal — review in Task Manager → Startup apps.</span>
    <span class="ja">赤=IT管理（無効化禁止）。黄=個人アプリ — タスクマネージャー → スタートアップで確認。</span>
  </div>
</div>

<div class="sec">
  <div class="sec-title">📊 <span class="en">RAM usage — top processes (Total: {ram} GB)</span><span class="ja">RAM使用量 上位プロセス（合計: {ram} GB）</span></div>
  <div class="tbl-wrap"><table>
    <thead><tr>
      <th><span class="en">Process</span><span class="ja">プロセス</span></th>
      <th><span class="en">Memory usage</span><span class="ja">メモリ使用量</span></th>
    </tr></thead>
    <tbody>{proc_rows}</tbody>
  </table></div>
</div>

<div class="footer">
  PC Health Agent v0.1 Demo &nbsp;·&nbsp; {data['generated_at']} &nbsp;·&nbsp; {data['computer']}<br>
  <span class="en">No data was sent anywhere. All analysis ran locally on this PC.</span>
  <span class="ja">データは外部に送信されていません。分析はすべてこのPC上でローカルに実行されました。</span>
</div>

<script>{JS}</script>
</body>
</html>"""
