param([switch]$Offline)
$ErrorActionPreference = 'Stop'
$env:PIP_DISABLE_PIP_VERSION_CHECK = '1'
$env:PIP_DEFAULT_TIMEOUT = '30'
$projectRoot = Split-Path -Parent $PSScriptRoot
& (Join-Path $PSScriptRoot 'verify-package.ps1') -Root $projectRoot
if (-not (Test-Path -LiteralPath (Join-Path $projectRoot '.gitignore'))) {
  $ignoreTemplate = Join-Path $projectRoot 'gitignore.template'
  if (Test-Path -LiteralPath $ignoreTemplate) { Copy-Item -LiteralPath $ignoreTemplate -Destination (Join-Path $projectRoot '.gitignore') }
}
Set-Location -LiteralPath $projectRoot
$env:TEMP = Join-Path $projectRoot 'tmp'
$env:TMP = $env:TEMP
New-Item -ItemType Directory -Force -Path $env:TEMP | Out-Null
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
  & python -m venv (Join-Path $projectRoot '.venv')
  if ($LASTEXITCODE -ne 0) { throw 'Python 3.10+ is required.' }
}
$config = Join-Path $projectRoot 'private\config.json'
if (Test-Path -LiteralPath $config) {
  & $python -I -m xiaojiu_brain --config $config status *> $null
  if ($LASTEXITCODE -eq 0) { throw 'The brain is running. Back up and stop it before upgrading files.' }
}
$wheelDir = Join-Path $projectRoot 'wheels'
$bundled = @(Get-ChildItem -LiteralPath $wheelDir -Filter 'xiaojiu_brain-*.whl' -ErrorAction SilentlyContinue)
if ($bundled.Count -gt 1) { throw 'Multiple kernel wheels found; use an intact release package.' }
$installSource = $projectRoot
if ($bundled.Count -eq 1) { $installSource = $bundled[0].FullName }
$pipArgs = @('-m', 'pip', 'install', '--no-cache-dir')
if ($Offline) { $pipArgs += @('--no-index', '--find-links', $wheelDir) }
$pipArgs += $installSource
& $python @pipArgs
if ($LASTEXITCODE -ne 0) { throw 'Package installation failed.' }
$config = Join-Path $projectRoot 'private\config.json'
if (-not (Test-Path -LiteralPath $config)) {
  $freePort = $null
  foreach ($candidatePort in 18880..18889) {
    $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, $candidatePort)
    try { $listener.Start(); $freePort = $candidatePort; break } catch {} finally { $listener.Stop() }
  }
  if ($null -eq $freePort) { throw 'No free candidate port in 18880..18889. Existing services were not stopped.' }
  & $python -m xiaojiu_brain --config $config init --data-dir '../data' --port $freePort
  if ($LASTEXITCODE -ne 0) { throw 'Configuration initialization failed.' }
}
Write-Host 'Installed with a fresh local configuration. No memories or model keys were imported.'
Write-Host 'Open the brain launcher for the panel, or use the DSH connection launcher.'
