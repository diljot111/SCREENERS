<#
serve_public.ps1
================
Make the hosted dashboard's WhatsApp/sending work by exposing your LOCAL backend
to the internet through a Cloudflare quick tunnel, then printing the URL to open.

What it starts:
  1. whatsapp-service (Node / Baileys)   -> http://localhost:3001
  2. web_server.py    (dashboard API)    -> http://localhost:8000
  3. cloudflared quick tunnel            -> https://<random>.trycloudflare.com

Then open:  https://screeners-hgg6.vercel.app/?api=<that tunnel URL>

Requirements: Node deps installed (whatsapp-service/node_modules), the Python
venv at python-engine/.venv, and cloudflared (winget install Cloudflare.cloudflared).

Run from anywhere:  powershell -ExecutionPolicy Bypass -File scripts\serve_public.ps1
Press Ctrl+C to stop everything.
#>

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$vercel = "https://screeners-hgg6.vercel.app"

function Find-Cloudflared {
  $c = (Get-Command cloudflared -ErrorAction SilentlyContinue).Source
  if ($c) { return $c }
  foreach ($p in @("$env:ProgramFiles\cloudflared\cloudflared.exe",
                   "${env:ProgramFiles(x86)}\cloudflared\cloudflared.exe")) {
    if (Test-Path $p) { return $p }
  }
  throw "cloudflared not found. Install it:  winget install Cloudflare.cloudflared"
}

$python = Join-Path $root "python-engine\.venv\Scripts\python.exe"
$cloudflared = Find-Cloudflared
$logDir = Join-Path $env:TEMP "screener-serve"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$cfLog = Join-Path $logDir "cloudflared.log"

Write-Host "Starting WhatsApp service (port 3001)..." -ForegroundColor Cyan
Start-Process -WindowStyle Minimized -WorkingDirectory (Join-Path $root "whatsapp-service") `
  -FilePath "node" -ArgumentList "index.js"

Write-Host "Starting dashboard API (port 8000)..." -ForegroundColor Cyan
Start-Process -WindowStyle Minimized -WorkingDirectory (Join-Path $root "python-engine") `
  -FilePath $python -ArgumentList "web_server.py", "8000"

Write-Host "Waiting for the dashboard to warm its cache (~20-40s)..." -ForegroundColor Cyan
for ($i = 0; $i -lt 40; $i++) {
  try { Invoke-WebRequest "http://localhost:8000/api/health" -TimeoutSec 3 -UseBasicParsing | Out-Null; break }
  catch { Start-Sleep -Seconds 2 }
}

Write-Host "Starting Cloudflare tunnel..." -ForegroundColor Cyan
if (Test-Path $cfLog) { Remove-Item $cfLog -Force }
Start-Process -WindowStyle Minimized -FilePath $cloudflared `
  -ArgumentList "tunnel", "--url", "http://localhost:8000", "--no-autoupdate" `
  -RedirectStandardError $cfLog -RedirectStandardOutput "$cfLog.out"

$url = $null
for ($i = 0; $i -lt 30; $i++) {
  Start-Sleep -Seconds 2
  if (Test-Path $cfLog) {
    $m = Select-String -Path $cfLog -Pattern "https://[a-z0-9-]+\.trycloudflare\.com" -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($m) { $url = $m.Matches[0].Value; break }
  }
}

if (-not $url) { Write-Host "Could not detect the tunnel URL. Check $cfLog" -ForegroundColor Red; exit 1 }

# wait until the public URL actually resolves
Write-Host "Tunnel: $url  (waiting for it to go live...)" -ForegroundColor Cyan
for ($i = 0; $i -lt 20; $i++) {
  try { Invoke-WebRequest "$url/api/health" -TimeoutSec 6 -UseBasicParsing | Out-Null; break }
  catch { Start-Sleep -Seconds 5 }
}

$open = "$vercel/?api=$url"
Write-Host ""
Write-Host "============================================================" -ForegroundColor Green
Write-Host " Backend is public. Open the hosted dashboard with WhatsApp:" -ForegroundColor Green
Write-Host ""
Write-Host "   $open" -ForegroundColor Yellow
Write-Host ""
Write-Host " (Or run locally instead: http://localhost:8000 )" -ForegroundColor DarkGray
Write-Host " Keep this window open. Ctrl+C to stop the tunnel." -ForegroundColor DarkGray
Write-Host "============================================================" -ForegroundColor Green

# keep the tunnel attached to this window
Get-Content $cfLog -Wait
