# Olist Analytics — starts PostgreSQL + the local web UI.
# Usage: powershell -ExecutionPolicy Bypass -File webapp\run.ps1

$ErrorActionPreference = 'Stop'

$bin    = 'C:\Users\ansar\AppData\Local\Programs\Python\Python312\Lib\site-packages\pgserver\pginstall\bin'
$pgdata = 'C:\Users\ansar\AppData\Local\Temp\opencode\pgdata-olist'
$logdir = 'C:\Users\ansar\AppData\Local\Temp\opencode'
$port   = 55432

# 1. PostgreSQL
$ready = $false
try { $ready = (& "$bin\pg_isready.exe" -h 127.0.0.1 -p $port) -match 'accepting' } catch { $ready = $false }

if (-not $ready) {
    Write-Host "Starting PostgreSQL on port $port ..."
    Start-Process -FilePath "$bin\postgres.exe" `
        -ArgumentList '-D', "`"$pgdata`"", '-p', "$port", '-c', 'listen_addresses=127.0.0.1' `
        -WindowStyle Hidden `
        -RedirectStandardOutput "$logdir\pg.out.log" -RedirectStandardError "$logdir\pg.err.log"

    for ($i = 0; $i -lt 40; $i++) {
        Start-Sleep -Milliseconds 500
        if ((& "$bin\pg_isready.exe" -h 127.0.0.1 -p $port) -match 'accepting') { break }
    }
}
Write-Host "PostgreSQL ready on 127.0.0.1:$port (database: olist)"

# 2. Web UI (blocks until Ctrl+C)
Write-Host "Olist Analytics -> http://127.0.0.1:5050"
python (Join-Path $PSScriptRoot 'app.py')
