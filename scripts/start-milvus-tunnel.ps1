param(
    [string]$Server = $env:DATA_AGENT_SSH_SERVER,
    [string]$KeyPath = $env:DATA_AGENT_SSH_KEY_PATH,
    [int]$MilvusPort = 19530,
    [int]$EmbeddingPort = 8081,
    [int]$RerankerPort = 8082,
    [int]$PostgresPort = 15433,
    [int]$MaxRetrySeconds = 30
)

if (-not $Server) { throw "DATA_AGENT_SSH_SERVER or -Server is required." }
if (-not $KeyPath -or -not (Test-Path -LiteralPath $KeyPath -PathType Leaf)) {
    throw "DATA_AGENT_SSH_KEY_PATH or -KeyPath must reference an existing private key."
}

$sshArguments = @(
    "-i", $KeyPath,
    "-o", "BatchMode=yes",
    "-o", "ExitOnForwardFailure=yes",
    "-o", "ServerAliveInterval=30",
    "-o", "ServerAliveCountMax=3",
    "-o", "ConnectTimeout=10",
    "-o", "TCPKeepAlive=yes",
    "-N",
    "-L", "127.0.0.1:$MilvusPort`:127.0.0.1:19530",
    "-L", "127.0.0.1:$EmbeddingPort`:127.0.0.1:8081",
    "-L", "127.0.0.1:$RerankerPort`:127.0.0.1:8082",
    "-L", "127.0.0.1:$PostgresPort`:127.0.0.1:5433",
    $Server
)

$retrySeconds = 2
while ($true) {
    Write-Host "Opening secure service tunnels: Milvus=$MilvusPort, embedding=$EmbeddingPort, reranker=$RerankerPort, PostgreSQL=$PostgresPort ..."
    & ssh @sshArguments
    $exitCode = $LASTEXITCODE
    Write-Warning "SSH tunnel stopped (exit code $exitCode). Reconnecting in $retrySeconds seconds..."
    Start-Sleep -Seconds $retrySeconds
    $retrySeconds = [Math]::Min($retrySeconds * 2, $MaxRetrySeconds)
}
