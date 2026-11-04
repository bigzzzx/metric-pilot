param(
    [string]$Server = $env:DATA_AGENT_SSH_SERVER,
    [string]$KeyPath = $env:DATA_AGENT_SSH_KEY_PATH,
    [ValidateRange(20000, 40000)][int]$Port = 25433
)
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
foreach ($taskLine in Get-Content -LiteralPath (Join-Path $taskRoot '.env')) {
    if (-not $Server -and $taskLine -match '^DATA_AGENT_SSH_SERVER=(.*)$') { $Server = $matches[1].Trim().Trim('"') }
    if (-not $KeyPath -and $taskLine -match '^DATA_AGENT_SSH_KEY_PATH=(.*)$') { $KeyPath = $matches[1].Trim().Trim('"') }
}
if ($Server -notmatch '^[A-Za-z0-9_.@:-]+$' -or -not (Test-Path -LiteralPath $KeyPath -PathType Leaf)) {
    throw 'An explicit SSH server and an existing key path are required.'
}
if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) {
    throw "Local port $Port is already in use; choose another acceptance port."
}
$taskContainer = 'metric-pilot-acceptance-' + $PID
$taskRandom = [Security.Cryptography.RandomNumberGenerator]::Create()
$taskBytes = New-Object byte[] 24
$taskRandom.GetBytes($taskBytes)
$taskRandom.Dispose()
$taskPassword = [BitConverter]::ToString($taskBytes).Replace('-', '')
$taskSshArgs = @('-i', $KeyPath, '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8')
$taskTunnel = $null
$taskStarted = $false
$taskPreviousDsn = $env:METRIC_PILOT_TEST_PG_DSN
try {
    # Password travels through SSH stdin, never command arguments, output, files or Git.
    $taskRemote = 'read -r POSTGRES_PASSWORD; POSTGRES_PASSWORD=$(printf "%s" "$POSTGRES_PASSWORD" | tr -d "\r"); export POSTGRES_PASSWORD; docker run --rm -d --name ' + $taskContainer +
        ' --label metric-pilot-purpose=acceptance --memory=256m --cpus=1 --tmpfs /var/lib/postgresql/data:rw,size=256m' +
        ' -p 127.0.0.1:' + $Port + ':5432 -e POSTGRES_USER=mp_owner -e POSTGRES_DB=metric_pilot_acceptance -e POSTGRES_PASSWORD postgres:16'
    $taskContainerId = $taskPassword | & ssh @taskSshArgs $Server $taskRemote
    if ($LASTEXITCODE -ne 0) { throw 'Could not start disposable PostgreSQL; no existing service was changed.' }
    $taskStarted = $true
    $taskReady = $false
    for ($taskAttempt = 0; $taskAttempt -lt 20; $taskAttempt++) {
        & ssh @taskSshArgs $Server "docker exec $taskContainer pg_isready -U mp_owner -d metric_pilot_acceptance" | Out-Null
        if ($LASTEXITCODE -eq 0) { $taskReady = $true; break }
        Start-Sleep -Seconds 1
    }
    if (-not $taskReady) { throw 'Disposable PostgreSQL did not become ready.' }
    $taskTunnelArgs = @('-i', ('"' + $KeyPath + '"'), '-o', 'BatchMode=yes', '-o', 'ExitOnForwardFailure=yes', '-N',
        '-L', "127.0.0.1:${Port}:127.0.0.1:$Port", $Server)
    $taskTunnel = Start-Process -FilePath 'ssh' -ArgumentList $taskTunnelArgs -PassThru -WindowStyle Hidden
    Start-Sleep -Seconds 2
    if ($taskTunnel.HasExited) { throw 'Acceptance SSH tunnel did not start.' }
    $env:METRIC_PILOT_TEST_PG_DSN = "host=127.0.0.1 port=$Port dbname=metric_pilot_acceptance user=mp_owner password=$taskPassword"
    $env:PYTHONIOENCODING = 'utf-8'
    & (Join-Path $taskRoot '.venv\Scripts\python.exe') (Join-Path $PSScriptRoot 'verify_data_platform.py') --postgres
    if ($LASTEXITCODE -ne 0) { throw 'PostgreSQL data platform acceptance failed.' }
} finally {
    $env:METRIC_PILOT_TEST_PG_DSN = $taskPreviousDsn
    $taskPassword = $null
    if ($taskTunnel -and -not $taskTunnel.HasExited) { Stop-Process -Id $taskTunnel.Id }
    if ($taskStarted) {
        # Only the uniquely named, disposable container created by this invocation.
        & ssh @taskSshArgs $Server "docker rm -f $taskContainer" | Out-Null
        Write-Host 'Disposable acceptance PostgreSQL and its in-memory data have been removed; existing containers were preserved.'
    }
}
