param([ValidateRange(20000,40000)][int]$MilvusPort=29530,
      [ValidateRange(20000,40000)][int]$EmbeddingPort=28081,
      [switch]$StartStoppedDependencies)
$ErrorActionPreference='Stop'
$taskRoot=Split-Path -Parent $PSScriptRoot
$taskSettings=@{}
foreach($line in Get-Content -LiteralPath (Join-Path $taskRoot '.env')) {
    if($line -match '^(DATA_AGENT_SSH_SERVER|DATA_AGENT_SSH_KEY_PATH)=(.*)$') {
        $taskSettings[$matches[1]]=$matches[2].Trim().Trim('"')
    }
}
$taskServer=$taskSettings['DATA_AGENT_SSH_SERVER']
$taskKey=$taskSettings['DATA_AGENT_SSH_KEY_PATH']
if($taskServer -notmatch '^[A-Za-z0-9_.@:-]+$' -or -not (Test-Path -LiteralPath $taskKey -PathType Leaf)) {
    throw 'Existing SSH server/key configuration is required.'
}
foreach($port in @($MilvusPort,$EmbeddingPort)) {
    if(Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue) {
        throw "Acceptance port $port is already occupied; refusing to reuse it."
    }
}
$taskPreviousUri=$env:MILVUS_URI
$taskPreviousEmbedding=$env:EMBEDDING_BASE_URL
$taskTunnel=$null
$taskStarted=@()
$taskSshArgs=@('-i',$taskKey,'-o','BatchMode=yes','-o','ConnectTimeout=8')
try {
    if($StartStoppedDependencies) {
        # Exact existing Data Agent containers only; preserve initial running state.
        $taskDependencies=@(
            @('milvus-etcd-dev','quay.io/coreos/etcd'),
            @('minio','minio/minio'),
            @('milvus','milvusdb/milvus'),
            @('data-agent-embedding','yuxi-api'))
        foreach($dependency in $taskDependencies) {
            $taskName=$dependency[0]
            $taskInfo=& ssh @taskSshArgs $taskServer "docker inspect --format '{{.Config.Image}} {{.State.Running}}' $taskName"
            if($LASTEXITCODE -ne 0 -or $taskInfo -notlike ($dependency[1]+'*')) { throw "Unexpected dependency $taskName; refusing to start." }
            if($taskInfo -match ' false$') {
                $taskStarted+=,$taskName
                & ssh @taskSshArgs $taskServer "docker start $taskName" | Out-Null
                if($LASTEXITCODE -ne 0) { throw "Could not start $taskName." }
            }
        }
        $taskReady=$false
        for($attempt=0;$attempt -lt 45;$attempt++) {
            & ssh @taskSshArgs $taskServer 'curl -fsS --max-time 2 http://127.0.0.1:9091/healthz >/dev/null 2>&1 && curl -fsS --max-time 2 http://127.0.0.1:8081/health >/dev/null 2>&1'
            if($LASTEXITCODE -eq 0) { $taskReady=$true; break }
            Start-Sleep -Seconds 1
        }
        if(-not $taskReady) { throw 'Milvus/BGE did not become ready.' }
    }
    $taskArgs=@('-i',('"'+$taskKey+'"'),'-o','BatchMode=yes','-o','ConnectTimeout=8',
        '-o','ExitOnForwardFailure=yes','-N','-L',"127.0.0.1:${MilvusPort}:127.0.0.1:19530",
        '-L',"127.0.0.1:${EmbeddingPort}:127.0.0.1:8081",$taskServer)
    $taskTunnel=Start-Process -FilePath 'ssh' -ArgumentList $taskArgs -PassThru -WindowStyle Hidden
    Start-Sleep -Seconds 2
    if($taskTunnel.HasExited) { throw 'Acceptance tunnel did not start.' }
    $env:MILVUS_URI="http://127.0.0.1:$MilvusPort"
    $env:EMBEDDING_BASE_URL="http://127.0.0.1:$EmbeddingPort"
    $env:PYTHONIOENCODING='utf-8'
    & (Join-Path $taskRoot '.venv\Scripts\python.exe') (Join-Path $PSScriptRoot 'verify_runtime_milvus.py')
    if($LASTEXITCODE -ne 0) { throw 'Isolated real Milvus acceptance failed.' }
} finally {
    $env:MILVUS_URI=$taskPreviousUri
    $env:EMBEDDING_BASE_URL=$taskPreviousEmbedding
    if($taskTunnel -and -not $taskTunnel.HasExited) { Stop-Process -Id $taskTunnel.Id }
    for($index=$taskStarted.Count-1;$index -ge 0;$index--) {
        $taskName=$taskStarted[$index]
        & ssh @taskSshArgs $taskServer "docker stop --time 10 $taskName" | Out-Null
    }
    if($taskStarted.Count -gt 0) { Write-Host 'Only acceptance-started dependencies were stopped again; other containers were preserved.' }
}
