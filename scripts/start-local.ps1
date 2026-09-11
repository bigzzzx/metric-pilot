param([switch]$SkipTunnel)

$projectRoot = Split-Path -Parent $PSScriptRoot
$dataDirectory = Join-Path $projectRoot ".data"
New-Item -ItemType Directory -Force -Path $dataDirectory | Out-Null

# Load only tunnel settings from .env. Other application settings are loaded by Python/Vite.
$envFile = Join-Path $projectRoot ".env"
if (Test-Path -LiteralPath $envFile) {
    foreach ($settingName in @("DATA_AGENT_SSH_SERVER", "DATA_AGENT_SSH_KEY_PATH")) {
        if (-not [Environment]::GetEnvironmentVariable($settingName)) {
            $line = Get-Content -LiteralPath $envFile | Where-Object { $_ -match "^$settingName=" } | Select-Object -Last 1
            if ($line) {
                [Environment]::SetEnvironmentVariable($settingName, $line.Substring($settingName.Length + 1))
            }
        }
    }
}

function Test-LocalPort([int]$Port) {
    return [bool](Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue)
}

if (-not $SkipTunnel) {
    $tunnelPorts = @(19530, 8081, 8082, 15433, 16379)
    $activeTunnelPorts = @($tunnelPorts | Where-Object { Test-LocalPort $_ })
    if ($activeTunnelPorts.Count -eq 0) {
        $tunnelScript = Join-Path $PSScriptRoot "start-milvus-tunnel.ps1"
        Start-Process powershell.exe `
            -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$tunnelScript`"" `
            -WorkingDirectory $projectRoot -WindowStyle Hidden `
            -RedirectStandardOutput (Join-Path $dataDirectory "service-tunnel.out.log") `
            -RedirectStandardError (Join-Path $dataDirectory "service-tunnel.err.log")
        Write-Host "SSH service tunnel supervisor started."
    } elseif ($activeTunnelPorts.Count -ne $tunnelPorts.Count) {
        Write-Warning "Only some tunnel ports are occupied; the existing processes were left untouched."
    }
}

if (-not (Test-LocalPort 18001)) {
    Start-Process (Join-Path $projectRoot ".venv\Scripts\python.exe") `
        -ArgumentList "scripts/start_api.py" -WorkingDirectory $projectRoot -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $dataDirectory "api.out.log") `
        -RedirectStandardError (Join-Path $dataDirectory "api.err.log")
    Write-Host "Backend started on http://127.0.0.1:18001."
}

if (-not (Test-LocalPort 5174)) {
    $node = (Get-Command node.exe -ErrorAction Stop).Source
    Start-Process $node `
        -ArgumentList "node_modules/vite/bin/vite.js --host 127.0.0.1 --port 5174 --strictPort" `
        -WorkingDirectory $projectRoot -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $dataDirectory "web.out.log") `
        -RedirectStandardError (Join-Path $dataDirectory "web.err.log")
    Write-Host "Frontend started on http://127.0.0.1:5174."
}

Write-Host "Local services requested. Check readiness with: npm run health"
