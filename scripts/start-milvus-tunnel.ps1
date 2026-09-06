param(
    [string]$Server = "ubuntu@152.136.236.79",
    [string]$KeyPath = "D:\Desktop\key\ssh key\ubuntu.pem",
    [int]$MilvusPort = 19530,
    [int]$EmbeddingPort = 8081,
    [int]$RerankerPort = 8082
)

$sshArguments = @(
    "-i", $KeyPath,
    "-o", "BatchMode=yes",
    "-o", "ExitOnForwardFailure=yes",
    "-o", "ServerAliveInterval=30",
    "-N",
    "-L", "127.0.0.1:$MilvusPort`:127.0.0.1:19530",
    "-L", "127.0.0.1:$EmbeddingPort`:127.0.0.1:8081",
    "-L", "127.0.0.1:$RerankerPort`:127.0.0.1:8082",
    $Server
)

Write-Host "Opening secure RAG tunnels: Milvus=$MilvusPort, embedding=$EmbeddingPort, reranker=$RerankerPort ..."
Write-Host "Keep this terminal open while running the local backend."
& ssh @sshArguments
