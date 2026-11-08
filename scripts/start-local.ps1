param([switch]$NoOpen)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$configPath = Join-Path $projectRoot 'private\config.json'
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) { throw 'Run the installer first.' }
if (-not (Test-Path -LiteralPath $configPath)) { throw 'Local configuration is missing.' }
$config = Get-Content -LiteralPath $configPath -Raw -Encoding UTF8 | ConvertFrom-Json
$secretPath = Join-Path $projectRoot 'private\model-secrets.dpapi'
if (Test-Path -LiteralPath $secretPath) {
  Add-Type -AssemblyName System.Security
  $protected = [IO.File]::ReadAllBytes($secretPath)
  $clear = [Security.Cryptography.ProtectedData]::Unprotect($protected, $null, [Security.Cryptography.DataProtectionScope]::CurrentUser)
  try {
    $secrets = [Text.Encoding]::UTF8.GetString($clear) | ConvertFrom-Json
    foreach ($name in @([string]$config.llm_key_env, [string]$config.embed_key_env)) {
      $property = $secrets.PSObject.Properties[$name]
      if ($property -and $property.Value) { [Environment]::SetEnvironmentVariable($name, [string]$property.Value, 'Process') }
    }
  } finally { [Array]::Clear($clear, 0, $clear.Length) }
}
$env:XIAOJIU_BRAIN_TOKEN = [string]$config.api_token
$env:PYTHONUTF8 = '1'
$env:TEMP = Join-Path $projectRoot 'tmp'
$env:TMP = $env:TEMP
New-Item -ItemType Directory -Force -Path $env:TEMP | Out-Null
$env:NO_PROXY = '127.0.0.1,localhost'
$env:no_proxy = '127.0.0.1,localhost'
$hostSettings = Join-Path $projectRoot 'private\host.json'
if (Test-Path -LiteralPath $hostSettings) {
  $hostConfig = Get-Content -LiteralPath $hostSettings -Raw -Encoding UTF8 | ConvertFrom-Json
  $embedding = $hostConfig.embeddingAutostart
  if ($embedding) {
    $socket = New-Object Net.Sockets.TcpClient
    $up = $false
    try { $connect = $socket.ConnectAsync('127.0.0.1', [int]$embedding.port); if ($connect.Wait(500)) { $up = $socket.Connected } } catch {} finally { $socket.Dispose() }
    if (-not $up) {
      $logs = Join-Path $projectRoot 'private\live\data\logs'
      New-Item -ItemType Directory -Force -Path $logs | Out-Null
      if (-not (Test-Path -LiteralPath $embedding.executable)) { throw 'Embedding executable is missing.' }
      Start-Process -FilePath $embedding.executable -ArgumentList @($embedding.arguments) -WindowStyle Hidden -RedirectStandardOutput (Join-Path $logs 'embedding.out.log') -RedirectStandardError (Join-Path $logs 'embedding.err.log') | Out-Null
      $deadline = (Get-Date).AddSeconds(60)
      do {
        Start-Sleep -Milliseconds 500
        $socket = New-Object Net.Sockets.TcpClient
        try { $connect = $socket.ConnectAsync('127.0.0.1', [int]$embedding.port); if ($connect.Wait(300)) { $up = $socket.Connected } } catch {} finally { $socket.Dispose() }
      } while (-not $up -and (Get-Date) -lt $deadline)
      if (-not $up) { Write-Warning 'Embedding service is not ready; the brain will report lexical-only retrieval.' }
    }
  }
}
# Import from the installed wheel, not from a mutable source checkout.
Push-Location (Split-Path -Parent $configPath)
try {
  $arguments = @('-X', 'utf8', '-m', 'xiaojiu_brain', '--config', $configPath, 'start')
  & $pythonPath @arguments
  if ($LASTEXITCODE -ne 0) { throw 'The local brain did not become ready. See its data/logs/service.log.' }
} finally { Pop-Location }
if (-not $NoOpen) {
  & powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File (Join-Path $PSScriptRoot 'open-panel.ps1')
  if ($LASTEXITCODE -ne 0) { throw 'The panel launcher reported a visible error. See data/logs/panel-launch.log.' }
}
