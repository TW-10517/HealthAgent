#Requires -Version 5.1
param(
    [switch]$Silent,
    [switch]$NoDupes
)
Set-StrictMode -Off
$ErrorActionPreference = "SilentlyContinue"

$AgentDir   = "$env:LOCALAPPDATA\PCHealthAgent"
$ReportsDir = "$AgentDir\reports"
$LogDir     = "$AgentDir\logs"
foreach ($d in $AgentDir,$ReportsDir,$LogDir) {
    if (-not (Test-Path $d)) { New-Item -ItemType Directory $d -Force | Out-Null }
}

$ProtectedPaths = @(
    $env:OneDrive,
    $env:OneDriveCommercial,
    "$env:USERPROFILE\OneDrive",
    "C:\Windows",
    "C:\Program Files",
    "C:\Program Files (x86)",
    "$env:PROGRAMDATA\ESET",
    "$env:LOCALAPPDATA\ESET",
    "$env:PROGRAMDATA\Optima",
    "$env:LOCALAPPDATA\Optima"
) | Where-Object { $_ }

function IsProtected([string]$p) {
    foreach ($x in $ProtectedPaths) { if ($p -like "$x*") { return $true } }
    return $false
}
function FmtSize([long]$b) {
    if ($b -ge 1GB) { return "{0:N1} GB" -f ($b/1GB) }
    if ($b -ge 1MB) { return "{0:N0} MB" -f ($b/1MB) }
    if ($b -ge 1KB) { return "{0:N0} KB" -f ($b/1KB) }
    return "$b B"
}
function DirSize([string]$path) {
    if (-not (Test-Path $path)) { return [long]0 }
    [long](Get-ChildItem $path -Recurse -File -EA SilentlyContinue | Measure-Object Length -Sum).Sum
}

# ── SCAN 1 — Disk ─────────────────────────────────────────────────────────────
Write-Host "  [1/5] Disk..." -ForegroundColor Cyan
$drv       = Get-PSDrive C
$diskFree  = [math]::Round($drv.Free/1GB,1)
$diskUsed  = [math]::Round($drv.Used/1GB,1)
$diskTotal = [math]::Round(($drv.Free+$drv.Used)/1GB,1)
$diskPct   = [math]::Round($drv.Free/($drv.Free+$drv.Used)*100,0)

# ── SCAN 2 — Temp/cache ───────────────────────────────────────────────────────
Write-Host "  [2/5] Temp and cache..." -ForegroundColor Cyan
$tempItems = [System.Collections.Generic.List[hashtable]]::new()

$scanDefs = @(
    "Temp files (%TEMP%)|$env:TEMP|true|temp",
    "Windows Temp|C:\Windows\Temp|true|temp",
    "npm cache|$env:APPDATA\npm-cache|true|cache",
    "pip cache|$env:LOCALAPPDATA\pip\Cache|true|cache",
    "Yarn cache|$env:LOCALAPPDATA\Yarn\Cache|true|cache",
    "VS Code logs|$env:APPDATA\Code\logs|true|log",
    "Chrome cache|$env:LOCALAPPDATA\Google\Chrome\User Data\Default\Cache|true|cache",
    "Edge cache|$env:LOCALAPPDATA\Microsoft\Edge\User Data\Default\Cache|true|cache",
    "Windows Error Reports|$env:LOCALAPPDATA\Microsoft\Windows\WER|true|log",
    "Thumbnail cache|$env:LOCALAPPDATA\Microsoft\Windows\Explorer|true|cache"
)

$jpNames = @{
    "Temp files (%TEMP%)"    = "&#19968;&#26178;&#12501;&#12449;&#12452;&#12523; (%TEMP%)"
    "Windows Temp"           = "Windows &#19968;&#26178;&#12501;&#12449;&#12452;&#12523;"
    "npm cache"              = "npm &#12461;&#12515;&#12483;&#12471;&#12517;"
    "pip cache"              = "pip &#12461;&#12515;&#12483;&#12471;&#12517;"
    "Yarn cache"             = "Yarn &#12461;&#12515;&#12483;&#12471;&#12517;"
    "VS Code logs"           = "VS Code &#12525;&#12464;"
    "Chrome cache"           = "Chrome &#12461;&#12515;&#12483;&#12471;&#12517;"
    "Edge cache"             = "Edge &#12461;&#12515;&#12483;&#12471;&#12517;"
    "Windows Error Reports"  = "Windows&#12456;&#12521;&#12540;&#12524;&#12509;&#12540;&#12488;"
    "Thumbnail cache"        = "&#12469;&#12512;&#12493;&#12452;&#12523;&#12461;&#12515;&#12483;&#12471;&#12517;"
}

foreach ($def in $scanDefs) {
    $parts = $def -split '\|'
    $name  = $parts[0]; $path = $parts[1]; $safe = $true; $cat = $parts[3]
    if (-not (Test-Path $path)) { continue }
    if (IsProtected $path)      { continue }
    $sz = DirSize $path
    if ($sz -gt 0) {
        $tempItems.Add(@{ Name=$name; NameJP=$jpNames[$name]; Path=$path; SizeBytes=$sz; Safe=$safe; Category=$cat })
    }
}
[long]$totalTempBytes = ($tempItems | Measure-Object SizeBytes -Sum).Sum

# ── SCAN 3 — Startup apps ─────────────────────────────────────────────────────
Write-Host "  [3/5] Startup apps..." -ForegroundColor Cyan
$startupApps = [System.Collections.Generic.List[hashtable]]::new()
$regPaths = @(
    "HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Run",
    "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Run"
)
foreach ($rp in $regPaths) {
    if (-not (Test-Path $rp)) { continue }
    (Get-ItemProperty $rp).PSObject.Properties | Where-Object { $_.Name -notmatch "^PS" } | ForEach-Object {
        $isIT = $_.Name -match "ESET|OPTIMA|Acall|OneDrive|Teams|MicrosoftEdge|SecurityHealth"
        $startupApps.Add(@{ Name=$_.Name; Command=$_.Value; ITManaged=$isIT })
    }
}
$itApps       = @($startupApps | Where-Object { $_.ITManaged })
$personalApps = @($startupApps | Where-Object { -not $_.ITManaged })

# ── SCAN 4 — RAM ──────────────────────────────────────────────────────────────
Write-Host "  [4/5] RAM processes..." -ForegroundColor Cyan
$topProcs = @(Get-Process | Sort-Object WorkingSet64 -Desc | Select-Object -First 8 | ForEach-Object {
    @{ Name=$_.ProcessName; MemMB=[math]::Round($_.WorkingSet64/1MB,0) }
})
$totalRAM = [math]::Round((Get-CimInstance Win32_ComputerSystem -EA SilentlyContinue).TotalPhysicalMemory/1GB,0)
if ($totalRAM -eq 0) { $totalRAM = 32 }

# ── SCAN 5 — Duplicates in Downloads ─────────────────────────────────────────
$dupeGroups     = @()
[long]$totalDupeBytes = 0
if (-not $NoDupes) {
    Write-Host "  [5/5] Duplicate scan (Downloads)..." -ForegroundColor Cyan
    $dlPath = "$env:USERPROFILE\Downloads"
    if ((Test-Path $dlPath) -and (-not (IsProtected $dlPath))) {
        $candidates = @(Get-ChildItem $dlPath -Recurse -File -EA SilentlyContinue |
            Where-Object { $_.Length -gt 50KB } |
            Where-Object { $_.FullName -notmatch "node_modules|\.venv|\.git" } |
            Select-Object -First 1500)
        $hashMap = @{}
        $done = 0
        foreach ($f in $candidates) {
            $done++
            if ($done % 200 -eq 0) { Write-Host "    $done / $($candidates.Count)" -ForegroundColor DarkGray }
            try {
                $h = (Get-FileHash $f.FullName -Algorithm MD5 -EA Stop).Hash
                if (-not $hashMap.ContainsKey($h)) { $hashMap[$h] = [System.Collections.Generic.List[object]]::new() }
                $hashMap[$h].Add($f)
            } catch {}
        }
        $dupeGroups = @($hashMap.Values | Where-Object { $_.Count -gt 1 } | ForEach-Object {
            $g = @($_)
            [long]$tot = ($g | Measure-Object Length -Sum).Sum
            [long]$big = ($g | Measure-Object Length -Maximum).Maximum
            @{ Files=$g; Count=$g.Count; TotalBytes=$tot; SaveableBytes=($tot-$big) }
        })
        [long]$totalDupeBytes = ($dupeGroups | Measure-Object SaveableBytes -Sum).Sum
    }
} else {
    Write-Host "  [5/5] Duplicate scan skipped (-NoDupes)." -ForegroundColor DarkGray
}

# ── Summary ───────────────────────────────────────────────────────────────────
$reportDate    = Get-Date -Format "yyyy-MM-dd HH:mm"
$reportFile    = "$ReportsDir\report-$(Get-Date -Format 'yyyyMMdd-HHmmss').html"
[long]$totalFreeable = $totalTempBytes + $totalDupeBytes
$issueCount = 0
if ($diskPct -lt 20)               { $issueCount++ }
if ($totalTempBytes -gt 500MB)     { $issueCount++ }
if ($totalDupeBytes -gt 100MB)     { $issueCount++ }
if ($personalApps.Count -gt 5)     { $issueCount++ }
if ($topProcs.Count -gt 0 -and $topProcs[0].MemMB -gt 1500) { $issueCount++ }

# ── HTML strings with Japanese as HTML entities (safe in any encoding) ────────
# Japanese labels are pre-encoded as HTML entities so no Japanese bytes in the .ps1
$lbl = @{
    freeSpace    = "&#31354;&#12365;&#23481;&#37327; / Free space"
    reclaimable  = "&#35299;&#25918;&#21487;&#33021; / Reclaimable"
    tempFiles    = "&#19968;&#26178;&#12501;&#12449;&#12452;&#12523; / Temp files"
    duplicates   = "&#37325;&#35079; / Duplicates"
    startupApps  = "&#36215;&#21205;&#12450;&#12503;&#12522; / Startup apps"
    ramTop       = "RAM &#20351;&#29992; / RAM top"
    perfTitle    = "&#12497;&#12501;&#12457;&#12540;&#12510;&#12531;&#12473;&#20998;&#26512; / Performance analysis"
    tempTitle    = "&#19968;&#26178;&#12539;&#12461;&#12515;&#12483;&#12471;&#12517;&#12501;&#12449;&#12452;&#12523; / Temp &amp; cache"
    dupeTitle    = "Downloads &#37325;&#35079;&#12501;&#12449;&#12452;&#12523; / Duplicate files"
    startupTitle = "&#36215;&#21205;&#12450;&#12503;&#12522;&#19968;&#35239; / Startup apps"
    ramTitle     = "RAM &#19978;&#20301;&#12503;&#12525;&#12475;&#12473; / Top RAM processes"
    footer1      = "&#12487;&#12540;&#12479;&#12399;&#22806;&#37096;&#12395;&#36865;&#20449;&#12373;&#12428;&#12390;&#12356;&#12414;&#12379;&#12435;&#12290;"
    footer2      = "&#20998;&#26512;&#12399;&#12377;&#12409;&#12390;&#12053;&#12398;PC&#19978;&#12391;&#12525;&#12540;&#12459;&#12523;&#12395;&#23455;&#34892;&#12373;&#12428;&#12414;&#12375;&#12383;&#12290;"
    itManaged    = "IT&#31649;&#29702; / IT-managed"
    personal     = "&#20491;&#20154; / Personal"
    skip         = "&#12473;&#12461;&#12483;&#12503; / Skip"
    review       = "&#30906;&#35469; / Review"
    safeNote     = "&#19978;&#35352;&#12399;&#12377;&#12409;&#12390;&#23433;&#20840;&#12395;&#21063;&#38500;&#12391;&#12365;&#12414;&#12539;&#12459;&#12483;&#12471;&#12517;&#12501;&#12449;&#12452;&#12523;&#12399;&#33258;&#21205;&#20877;&#29983;&#25104;&#12373;&#12428;&#12414;&#12377;&#12290;"
    startupNote  = "&#36951;&#12364;&#12356;&#12434;&#21270;&#12391;&#12365;&#12414;&#12377;&#12290;"
    groups       = "&#12464;&#12523;&#12540;&#12503;"
    saveable     = "&#31680;&#32039;&#21487;&#33021;"
    copies       = "&#12467;&#12500;&#12540;&#25968;"
    total        = "&#21512;&#35336;"
    location     = "&#22580;&#25152;"
    size         = "&#12469;&#12452;&#12474;"
    app          = "&#12450;&#12503;&#12522;"
    action       = "&#23550;&#24540;"
    command      = "&#12467;&#12510;&#12531;&#12489;"
    memory       = "&#12513;&#12514;&#12522;&#20351;&#29992;&#37327;"
    process      = "&#12503;&#12525;&#12475;&#12473;"
}

# ── Performance cards ─────────────────────────────────────────────────────────
$perfCards = [System.Collections.Generic.List[hashtable]]::new()
if ($startupApps.Count -gt 8) {
    $perfCards.Add(@{
        Icon   = "&#x1F680;"
        En_T   = "$($startupApps.Count) apps start with Windows — $($personalApps.Count) are personal"
        Jp_T   = "Windows&#36215;&#21205;&#26178;&#12395;$($startupApps.Count)&#20491;&#36215;&#21205; &#8212; $($personalApps.Count)&#20491;&#12399;&#20491;&#20154;&#12450;&#12503;&#12522;"
        En_D   = "Each startup app delays boot by a small amount. $($personalApps.Count) personal apps can be reviewed."
        Jp_D   = "&#36215;&#21205;&#12450;&#12503;&#12522;&#12399;&#36215;&#21205;&#26178;&#38291;&#12434;&#36938;&#12385;&#12414;&#12377;&#12290;$($personalApps.Count)&#20491;&#12398;&#20491;&#20154;&#12450;&#12503;&#12522;&#12434;&#35211;&#30452;&#12375;&#12390;&#12367;&#12384;&#12373;&#12356;&#12290;"
        En_F   = "Task Manager → Startup apps → disable what you don't need."
        Jp_F   = "&#12479;&#12473;&#12463;&#12510;&#12493;&#12540;&#12472;&#12515;&#12540; &rarr; &#12473;&#12479;&#12540;&#12488;&#12450;&#12483;&#12503;&#12289;&#19981;&#35201;&#12394;&#12418;&#12398;&#12434;&#28961;&#21177;&#21270;&#12290;"
        Level  = "yellow"
    })
}
$browserProc = @($topProcs | Where-Object { $_.Name -match "msedge|chrome|firefox" })[0]
if ($browserProc -and $browserProc.MemMB -gt 800) {
    $perfCards.Add(@{
        Icon  = "&#x1F310;"
        En_T  = "$($browserProc.Name) is using $($browserProc.MemMB) MB of RAM"
        Jp_T  = "$($browserProc.Name) &#12364; $($browserProc.MemMB) MB &#12434;&#20351;&#29992;&#20013;"
        En_D  = "High browser RAM usage. Close unused tabs or enable Sleeping Tabs."
        Jp_D  = "&#12502;&#12521;&#12454;&#12470;&#12398;RAM&#20351;&#29992;&#37327;&#12364;&#39640;&#12356;&#12290;&#26410;&#20351;&#29992;&#12479;&#12502;&#12434;&#38281;&#12058;&#12427;&#12363;&#12473;&#12522;&#12540;&#12500;&#12531;&#12464;&#12479;&#12502;&#12434;&#26377;&#21177;&#12395;&#12290;"
        En_F  = "Edge: Settings &rarr; System &rarr; Sleeping Tabs &rarr; Enable"
        Jp_F  = "Edge&#35373;&#23450; &rarr; &#12471;&#12473;&#12486;&#12512; &rarr; &#12473;&#12522;&#12540;&#12500;&#12531;&#12464;&#12479;&#12502; &rarr; &#26377;&#21177;&#12395;&#12377;&#12427;"
        Level = "yellow"
    })
}
if ($diskPct -lt 20) {
    $perfCards.Add(@{
        Icon  = "&#x1F4BE;"
        En_T  = "Disk critically low — $diskPct% free ($diskFree GB)"
        Jp_T  = "&#12487;&#12451;&#12473;&#12463;&#27243;&#37327;&#12364;&#21322;&#27005;&#24133;&#22580;&#8212;&#31354;&#12365; $diskPct% ($diskFree GB)"
        En_D  = "Windows needs at least 15% free disk for updates, swap, and temp operations."
        Jp_D  = "Windows&#12399;&#12450;&#12483;&#12503;&#12487;&#12540;&#12488;&#12539;&#12473;&#12527;&#12483;&#12503;&#12539;&#19968;&#26178;&#25805;&#20316;&#12395;&#26368;&#20302;15%&#12398;&#31354;&#12365;&#12364;&#24517;&#35201;&#12391;&#12377;&#12290;"
        En_F  = "Clean temp files and duplicates in this report."
        Jp_F  = "&#12053;&#12398;&#12524;&#12509;&#12540;&#12488;&#12398;&#19968;&#26178;&#12501;&#12449;&#12452;&#12523;&#12392;&#37325;&#35079;&#12434;&#21063;&#38500;&#12375;&#12390;&#23481;&#37327;&#12434;&#30830;&#20445;&#12375;&#12390;&#12367;&#12384;&#12373;&#12356;&#12290;"
        Level = "red"
    })
}
if ($totalTempBytes -gt 500MB) {
    $perfCards.Add(@{
        Icon  = "&#x1F5D1;&#xFE0F;"
        En_T  = "$(FmtSize $totalTempBytes) in temp and cache files"
        Jp_T  = "$(FmtSize $totalTempBytes) &#12398;&#19968;&#26178;&#12539;&#12461;&#12515;&#12483;&#12471;&#12517;&#12501;&#12449;&#12452;&#12523;"
        En_D  = "Accumulated temp files waste disk space and can slow down file operations."
        Jp_D  = "&#27809;&#31295;&#12375;&#12383;&#19968;&#26178;&#12501;&#12449;&#12452;&#12523;&#12399;&#12487;&#12451;&#12473;&#12463;&#23481;&#37327;&#12434;&#28112;&#36947;&#12395;&#28040;&#36027;&#12375;&#12414;&#12377;&#12290;"
        En_F  = "All temp items in this report are safe to delete."
        Jp_F  = "&#12524;&#12509;&#12540;&#12488;&#20869;&#12398;&#19968;&#26178;&#12501;&#12449;&#12452;&#12523;&#12399;&#12377;&#12409;&#12390;&#23433;&#20840;&#12395;&#21063;&#38500;&#12391;&#12365;&#12414;&#12377;&#12290;"
        Level = "yellow"
    })
}
if (-not $perfCards.Count) {
    $perfCards.Add(@{ Icon="&#x2705;"; En_T="No significant performance issues found"; Jp_T="&#12497;&#12501;&#12457;&#12540;&#12510;&#12531;&#12473;&#19978;&#12398;&#37325;&#22823;&#12394;&#554;&#38988;&#12399;&#35211;&#12388;&#12363;&#12426;&#12414;&#12379;&#12435;&#12391;&#12375;&#12383;"; En_D=""; Jp_D=""; En_F=""; Jp_F=""; Level="green" })
}

# ── Build HTML rows ───────────────────────────────────────────────────────────
function PerfCard($c) {
    $bg  = "var(--" + $c.Level + "-bg)"
    $bdr = "var(--" + $c.Level + "-border)"
    $detail = ""
    if ($c.En_D) {
        $detail  = '<div style="font-size:12px;color:var(--fg2)">' + $c.En_D + '<br><span style="color:var(--fg3)">' + $c.Jp_D + '</span></div>'
        $detail += '<div style="font-size:12px;font-weight:600;margin-top:6px">&rarr; ' + $c.En_F + '<br><span style="color:var(--fg3)">' + $c.Jp_F + '</span></div>'
    }
    return '<div style="background:' + $bg + ';border:1px solid ' + $bdr + ';border-radius:9px;padding:14px 16px;display:grid;grid-template-columns:auto 1fr;gap:12px">' +
           '<span style="font-size:20px">' + $c.Icon + '</span>' +
           '<div><div style="font-size:13px;font-weight:600;margin-bottom:2px">' + $c.En_T + '</div>' +
           '<div style="font-size:12px;color:var(--fg3);margin-bottom:6px">' + $c.Jp_T + '</div>' +
           $detail + '</div></div>'
}
$perfHTML = ""
foreach ($c in $perfCards) { $perfHTML += (PerfCard $c) + "`n" }

$tempRows = ""
foreach ($t in ($tempItems | Sort-Object SizeBytes -Desc)) {
    $col    = if ($t.SizeBytes -gt 500MB) { "var(--red)" } elseif ($t.SizeBytes -gt 100MB) { "var(--yellow)" } else { "var(--green)" }
    $ico    = if ($t.SizeBytes -gt 500MB) { "&#x1F534;" } elseif ($t.SizeBytes -gt 100MB) { "&#x1F7E1;" } else { "&#x1F7E2;" }
    $nameJP = if ($t.NameJP) { $t.NameJP } else { $t.Name }
    $sz     = FmtSize $t.SizeBytes
    $tempRows += '<tr><td>' + $ico + ' ' + $t.Name + '<br><small style="color:var(--fg3)">' + $nameJP + '</small></td><td><code>' + $t.Path + '</code></td><td style="font-weight:700;color:' + $col + '">' + $sz + '</td></tr>'
}

$dupeRows = ""
foreach ($g in ($dupeGroups | Sort-Object SaveableBytes -Desc | Select-Object -First 15)) {
    $names = ($g.Files | Select-Object -First 2 | ForEach-Object { $_.Name }) -join ", "
    if ($g.Files.Count -gt 2) { $names += " +$($g.Files.Count-2) more" }
    $dupeRows += '<tr><td>' + $names + '</td><td style="text-align:center">' + $g.Count + '</td><td>' + (FmtSize $g.TotalBytes) + '</td><td style="color:var(--green);font-weight:700">' + (FmtSize $g.SaveableBytes) + '</td></tr>'
}

$startupRows = ""
foreach ($a in ($startupApps | Sort-Object { -not $_.ITManaged })) {
    if ($a.ITManaged) {
        $badge  = '<span style="background:var(--red-chip);color:var(--red);font-size:10px;font-weight:700;padding:1px 7px;border-radius:100px">' + $lbl.itManaged + '</span>'
        $action = '<span style="color:var(--fg3);font-size:12px">' + $lbl.skip + '</span>'
    } else {
        $badge  = '<span style="background:var(--yellow-chip);color:var(--yellow);font-size:10px;font-weight:700;padding:1px 7px;border-radius:100px">' + $lbl.personal + '</span>'
        $action = '<span style="color:var(--yellow);font-size:12px;font-weight:600">' + $lbl.review + '</span>'
    }
    $cmd = if ($a.Command -and $a.Command.Length -gt 60) { $a.Command.Substring(0,60) + "..." } else { $a.Command }
    $startupRows += '<tr><td>' + $a.Name + ' ' + $badge + '</td><td style="font-size:11px;color:var(--fg3)"><code>' + $cmd + '</code></td><td>' + $action + '</td></tr>'
}

$procRows = ""
foreach ($p in $topProcs) {
    $pct = if ($totalRAM -gt 0) { [math]::Min([math]::Round($p.MemMB/($totalRAM*10.24),0),100) } else { 0 }
    $col = if ($p.MemMB -gt 1500) { "var(--red)" } elseif ($p.MemMB -gt 600) { "var(--yellow)" } else { "var(--green)" }
    $bar = '<div style="display:flex;align-items:center;gap:8px"><div style="flex:1;background:var(--surface2);border-radius:4px;height:6px"><div style="width:' + $pct + '%;background:' + $col + ';height:6px;border-radius:4px"></div></div><span style="font-weight:700;color:' + $col + ';min-width:64px;text-align:right">' + $p.MemMB + ' MB</span></div>'
    $procRows += '<tr><td>' + $p.Name + '</td><td>' + $bar + '</td></tr>'
}

$diskColor = if ($diskPct -lt 15){"var(--red)"}elseif($diskPct -lt 25){"var(--yellow)"}else{"var(--green)"}

$dupeSection = if ($NoDupes) {
    "<p style=`"color:var(--fg3);font-size:13px;padding:8px 0`">Duplicate scan skipped. Run without -NoDupes to enable. / &#37325;&#35079;&#12473;&#12461;&#12515;&#12531;&#12399;&#12473;&#12461;&#12483;&#12503;&#12373;&#12428;&#12414;&#12375;&#12383;&#12290;</p>"
} elseif ($dupeGroups.Count -eq 0) {
    "<p style=`"color:var(--fg3);font-size:13px;padding:8px 0`">No duplicates found in Downloads. / Downloads&#12395;&#37325;&#35079;&#12501;&#12449;&#12452;&#12523;&#12399;&#12354;&#12426;&#12414;&#12379;&#12435;&#12391;&#12375;&#12383;&#12290;</p>"
} else {
    "<p style=`"font-size:13px;color:var(--fg2);margin-bottom:12px`">Found <strong>$($dupeGroups.Count) $($lbl.groups)</strong> in Downloads &mdash; <strong style=`"color:var(--green)`">$(FmtSize $totalDupeBytes) $($lbl.saveable)</strong></p><div style=`"overflow-x:auto`"><table><thead><tr><th>Files</th><th>$($lbl.copies)</th><th>$($lbl.total)</th><th>$($lbl.saveable)</th></tr></thead><tbody>$dupeRows</tbody></table></div>"
}

# ── Assemble full HTML ─────────────────────────────────────────────────────────
$css = @'
:root{--bg:#f6f5f2;--surface:#fff;--surface2:#f0efe9;--border:#e4e2dc;--fg:#181715;--fg2:#6b6560;--fg3:#b0ada6;--accent:#1d6bde;--accent-bg:#eef4ff;--accent-border:#c3d9ff;--green:#15803d;--green-bg:#f0fdf4;--green-chip:#bbf7d0;--green-border:#86efac;--yellow:#92400e;--yellow-bg:#fffbeb;--yellow-chip:#fde68a;--yellow-border:#fcd34d;--red:#b91c1c;--red-bg:#fff1f1;--red-chip:#fecaca;--red-border:#fca5a5;--mono:'Consolas',monospace}
@media(prefers-color-scheme:dark){:root{--bg:#111110;--surface:#1c1b19;--surface2:#232220;--border:#2e2d2a;--fg:#f5f4f0;--fg2:#9e9890;--fg3:#5a5650;--accent:#5b9bff;--accent-bg:#0d1f3c;--accent-border:#1e3d6e;--green:#4ade80;--green-bg:#052e16;--green-chip:#14532d;--green-border:#166534;--yellow:#fbbf24;--yellow-bg:#1c1003;--yellow-chip:#451a03;--yellow-border:#78350f;--red:#f87171;--red-bg:#1f0505;--red-chip:#450a0a;--red-border:#7f1d1d;color-scheme:dark}}
*,*::before,*::after{box-sizing:border-box}
body{font-family:-apple-system,'Segoe UI',system-ui,sans-serif;background:var(--bg);color:var(--fg);margin:0;padding:24px 20px;font-size:14px;line-height:1.6;max-width:960px;margin-inline:auto}
h2{text-wrap:balance;margin:0}
code{font-family:var(--mono);font-size:11px}
.header{display:flex;align-items:center;justify-content:space-between;padding:16px 20px;background:var(--surface);border:1px solid var(--border);border-radius:10px;margin-bottom:20px;flex-wrap:wrap;gap:8px}
.badge{font-size:11px;font-weight:700;padding:4px 12px;border-radius:100px;background:var(--accent-bg);border:1px solid var(--accent-border);color:var(--accent)}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:8px;margin-bottom:24px}
.stat{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:12px 14px}
.stat .lbl{font-size:10px;color:var(--fg3);text-transform:uppercase;letter-spacing:.05em;margin-bottom:4px}
.stat .val{font-size:1.3rem;font-weight:700;font-variant-numeric:tabular-nums;font-family:var(--mono)}
.stat .sub{font-size:11px;color:var(--fg3);margin-top:2px}
.sec{margin-bottom:32px}
.sec-title{font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.06em;color:var(--fg3);margin-bottom:12px;padding-bottom:6px;border-bottom:1px solid var(--border)}
.perf-grid{display:flex;flex-direction:column;gap:8px}
table{width:100%;border-collapse:collapse;background:var(--surface);border:1px solid var(--border);border-radius:8px;overflow:hidden;font-size:13px}
thead th{background:var(--surface2);font-size:10px;text-transform:uppercase;letter-spacing:.05em;font-weight:600;color:var(--fg3);padding:8px 12px;text-align:left;border-bottom:1px solid var(--border)}
tbody tr{border-bottom:1px solid var(--border)}
tbody tr:last-child{border-bottom:none}
tbody td{padding:9px 12px;vertical-align:middle}
tbody tr:hover{background:var(--surface2)}
.note{font-size:12px;color:var(--fg3);margin-top:8px;padding:8px 12px;border-radius:6px}
.footer{font-size:11px;color:var(--fg3);text-align:center;margin-top:32px;padding-top:16px;border-top:1px solid var(--border)}
'@

$dupeColor  = if ($totalDupeBytes -gt 100MB) { "var(--yellow)" } else { "var(--fg3)" }
$startColor = if ($startupApps.Count -gt 8) { "var(--yellow)" } else { "var(--green)" }
$topMemMB   = if ($topProcs.Count) { $topProcs[0].MemMB } else { 0 }
$topMemName = if ($topProcs.Count) { $topProcs[0].Name  } else { "n/a" }

$parts = [System.Collections.Generic.List[string]]::new()
$parts.Add('<!DOCTYPE html><html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>PC Health Report</title><style>' + $css + '</style></head><body>')
$parts.Add('<div class="header"><div><h2 style="font-size:1.1rem">&#x1FA7A; PC Health Report</h2><div style="font-size:12px;color:var(--fg3);margin-top:2px">' + $env:COMPUTERNAME + ' &nbsp;&middot;&nbsp; ' + $env:USERNAME + ' &nbsp;&middot;&nbsp; ' + $reportDate + '</div></div><span class="badge">PC Health Agent v0.1 Demo</span></div>')
$parts.Add('<div class="stats">' +
  '<div class="stat"><div class="lbl">' + $lbl.freeSpace + '</div><div class="val" style="color:' + $diskColor + '">' + $diskFree + ' GB</div><div class="sub">' + $diskPct + '% of ' + $diskTotal + ' GB</div></div>' +
  '<div class="stat"><div class="lbl">' + $lbl.reclaimable + '</div><div class="val" style="color:var(--yellow)">' + (FmtSize $totalFreeable) + '</div><div class="sub">temp + dupes</div></div>' +
  '<div class="stat"><div class="lbl">' + $lbl.tempFiles + '</div><div class="val" style="color:var(--yellow)">' + (FmtSize $totalTempBytes) + '</div><div class="sub">' + $tempItems.Count + ' locations</div></div>' +
  '<div class="stat"><div class="lbl">' + $lbl.duplicates + '</div><div class="val" style="color:' + $dupeColor + '">' + (FmtSize $totalDupeBytes) + '</div><div class="sub">' + $dupeGroups.Count + ' ' + $lbl.groups + '</div></div>' +
  '<div class="stat"><div class="lbl">' + $lbl.startupApps + '</div><div class="val" style="color:' + $startColor + '">' + $startupApps.Count + '</div><div class="sub">' + $personalApps.Count + ' ' + $lbl.personal + '</div></div>' +
  '<div class="stat"><div class="lbl">' + $lbl.ramTop + '</div><div class="val">' + $topMemMB + ' MB</div><div class="sub">' + $topMemName + '</div></div>' +
  '</div>')
$parts.Add('<div class="sec"><div class="sec-title">&#x26A1; ' + $lbl.perfTitle + '</div><div class="perf-grid">' + $perfHTML + '</div></div>')
$parts.Add('<div class="sec"><div class="sec-title">&#x1F5D1;&#xFE0F; ' + $lbl.tempTitle + ' &mdash; ' + (FmtSize $totalTempBytes) + '</div><div style="overflow-x:auto"><table><thead><tr><th>' + $lbl.location + '</th><th>Path</th><th>' + $lbl.size + '</th></tr></thead><tbody>' + $tempRows + '</tbody></table></div><div class="note" style="background:var(--green-bg);border:1px solid var(--green-border)">&#x2705; All items above are safe to delete &mdash; ' + $lbl.safeNote + '</div></div>')
$parts.Add('<div class="sec"><div class="sec-title">&#x1F46F; ' + $lbl.dupeTitle + '</div>' + $dupeSection + '</div>')
$parts.Add('<div class="sec"><div class="sec-title">&#x1F680; ' + $lbl.startupTitle + ' &mdash; ' + $startupApps.Count + ' total</div><div style="overflow-x:auto"><table><thead><tr><th>' + $lbl.app + '</th><th>' + $lbl.command + '</th><th>' + $lbl.action + '</th></tr></thead><tbody>' + $startupRows + '</tbody></table></div><div class="note">IT-managed = never disable. Personal = review in Task Manager &rarr; Startup apps.</div></div>')
$parts.Add('<div class="sec"><div class="sec-title">&#x1F4CA; ' + $lbl.ramTitle + ' (RAM: ' + $totalRAM + ' GB)</div><div style="overflow-x:auto"><table><thead><tr><th>' + $lbl.process + '</th><th>' + $lbl.memory + ' (total: ' + $totalRAM + ' GB)</th></tr></thead><tbody>' + $procRows + '</tbody></table></div></div>')
$parts.Add('<div class="footer">PC Health Agent v0.1 Demo &nbsp;&middot;&nbsp; ' + $reportDate + ' &nbsp;&middot;&nbsp; ' + $env:COMPUTERNAME + '<br>' + $lbl.footer1 + ' ' + $lbl.footer2 + '</div>')
$parts.Add('</body></html>')
$html = $parts -join "`n"

# Write with explicit UTF-8
[System.IO.File]::WriteAllText($reportFile, $html, [System.Text.UTF8Encoding]::new($false))
Write-Host ""
Write-Host "  Report: $reportFile" -ForegroundColor Green

# ── Toast notification ─────────────────────────────────────────────────────────
$toastTitle = if ($issueCount -gt 0) { "PC Health: $issueCount issue$(if($issueCount -ne 1){'s'}) found" } else { "PC Health: All clear" }
$toastBody  = "$(FmtSize $totalFreeable) reclaimable. Click to open report."
try {
    [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] | Out-Null
    [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType=WindowsRuntime] | Out-Null
    $xml = [Windows.Data.Xml.Dom.XmlDocument]::new()
    $xml.LoadXml("<toast><visual><binding template=`"ToastGeneric`"><text>$toastTitle</text><text>$toastBody</text></binding></visual></toast>")
    $notifier = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("PC Health Agent")
    $notifier.Show([Windows.UI.Notifications.ToastNotification]::new($xml))
    Write-Host "  Toast notification sent." -ForegroundColor Green
} catch {
    Write-Host "  Toast: $($_.Exception.Message)" -ForegroundColor DarkGray
}

# ── Log ────────────────────────────────────────────────────────────────────────
"$reportDate | Free=$(FmtSize ($diskFree*1GB)) | Temp=$(FmtSize $totalTempBytes) | Dupes=$(FmtSize $totalDupeBytes) | Startup=$($startupApps.Count) | Issues=$issueCount" |
    Add-Content "$LogDir\scan-history.txt"

# ── Summary ────────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host ("=" * 55) -ForegroundColor Cyan
Write-Host " PC Health Agent  --  Scan complete" -ForegroundColor Cyan
Write-Host ("=" * 55) -ForegroundColor Cyan
Write-Host (" Issues       : $issueCount") -ForegroundColor $(if($issueCount){"Yellow"}else{"Green"})
Write-Host (" Reclaimable  : $(FmtSize $totalFreeable)") -ForegroundColor Yellow
Write-Host (" Startup apps : $($startupApps.Count)  ($($personalApps.Count) personal)") -ForegroundColor White
Write-Host (" Report       : $reportFile") -ForegroundColor White
Write-Host ("=" * 55) -ForegroundColor Cyan
Write-Host ""

if (-not $Silent) { Start-Process $reportFile }
