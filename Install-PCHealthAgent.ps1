#Requires -Version 5.1
<#
.SYNOPSIS
  Installs PC Health Agent — copies files to AppData and registers a weekly Task Scheduler task.
.NOTES
  Run once. The task then fires every Sunday at 09:00, or when disk < 15%.
  IMPORTANT: Before deploying company-wide, get ESET whitelist + OPTIMA registration from IT.
#>
param(
    [switch]$NoTask,      # Skip Task Scheduler registration (demo mode)
    [switch]$RunNow       # Run the agent immediately after install
)

$AgentDir = "$env:LOCALAPPDATA\PCHealthAgent"
$ScriptSrc = "$PSScriptRoot\PCHealthAgent.ps1"

# ── Verify source exists ──────────────────────────────────────────────────────
if (-not (Test-Path $ScriptSrc)) {
    Write-Host "ERROR: PCHealthAgent.ps1 not found next to this installer." -ForegroundColor Red
    Write-Host "       Expected: $ScriptSrc" -ForegroundColor Red
    exit 1
}

# ── Create agent directory ────────────────────────────────────────────────────
Write-Host "Installing PC Health Agent..." -ForegroundColor Cyan
New-Item -ItemType Directory $AgentDir -Force | Out-Null
New-Item -ItemType Directory "$AgentDir\reports" -Force | Out-Null
New-Item -ItemType Directory "$AgentDir\logs" -Force | Out-Null

# ── Copy agent script ─────────────────────────────────────────────────────────
Copy-Item $ScriptSrc "$AgentDir\PCHealthAgent.ps1" -Force
Write-Host "  Copied agent to $AgentDir" -ForegroundColor Green

# ── Write default config ──────────────────────────────────────────────────────
$config = @{
    Language          = "both"
    ScanFrequency     = "weekly"
    CleanupLevel      = "standard"
    SkipFolders       = @()
    NotificationStyle = "toast"
    InstalledAt       = (Get-Date -Format "yyyy-MM-dd HH:mm")
    Version           = "0.1"
} | ConvertTo-Json
$config | Out-File "$AgentDir\config.json" -Encoding UTF8
Write-Host "  Config written." -ForegroundColor Green

# ── Register Task Scheduler task ──────────────────────────────────────────────
if (-not $NoTask) {
    $taskName = "PCHealthAgent"
    $existing = Get-ScheduledTask -TaskName $taskName -EA SilentlyContinue

    $action = New-ScheduledTaskAction `
        -Execute "powershell.exe" `
        -Argument "-NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$AgentDir\PCHealthAgent.ps1`""

    # Weekly on Sunday 09:00
    $trigger1 = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At "09:00"

    # Also trigger when disk usage crosses 85% — approximated by a daily idle trigger
    $trigger2 = New-ScheduledTaskTrigger -Daily -At "08:00"

    $settings = New-ScheduledTaskSettingsSet `
        -RunOnlyIfIdle `
        -IdleDuration (New-TimeSpan -Minutes 5) `
        -IdleWaitTimeout (New-TimeSpan -Hours 1) `
        -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
        -MultipleInstances IgnoreNew

    $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -RunLevel Limited

    if ($existing) {
        Set-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger1,$trigger2 -Settings $settings | Out-Null
        Write-Host "  Task Scheduler: updated existing task '$taskName'." -ForegroundColor Green
    } else {
        Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger1,$trigger2 `
            -Settings $settings -Principal $principal -Description "PC Health Agent — weekly scan and cleanup suggestions." | Out-Null
        Write-Host "  Task Scheduler: registered '$taskName' (weekly Sunday 09:00)." -ForegroundColor Green
    }
} else {
    Write-Host "  Task Scheduler: skipped (-NoTask flag)." -ForegroundColor DarkGray
}

# ── Done ──────────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" -ForegroundColor Green
Write-Host " PC Health Agent installed successfully!" -ForegroundColor Green
Write-Host "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" -ForegroundColor Green
Write-Host " Agent location : $AgentDir" -ForegroundColor White
Write-Host " Reports saved  : $AgentDir\reports\" -ForegroundColor White
Write-Host " Scan history   : $AgentDir\logs\scan-history.txt" -ForegroundColor White
if (-not $NoTask) {
    Write-Host " Next auto-scan : Sunday 09:00 (Task Scheduler)" -ForegroundColor White
}
Write-Host "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" -ForegroundColor Green
Write-Host ""
Write-Host " ⚠️  BEFORE company-wide deployment, contact IT to:" -ForegroundColor Yellow
Write-Host "    1. Whitelist PCHealthAgent.exe in ESET" -ForegroundColor Yellow
Write-Host "    2. Register the scheduled task in OPTIMA Eye" -ForegroundColor Yellow
Write-Host ""

if ($RunNow) {
    Write-Host "Running first scan now..." -ForegroundColor Cyan
    & "$AgentDir\PCHealthAgent.ps1"
}
