param([switch]$Offline)
$ErrorActionPreference = 'Stop'
$env:PIP_DISABLE_PIP_VERSION_CHECK = '1'
$env:PIP_DEFAULT_TIMEOUT = '30'
$env:PYTHONUTF8 = '1'
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
$config = Join-Path $projectRoot 'private\config.json'
if (Test-Path -LiteralPath $config) {
  throw 'Existing configuration preserved. This installer does not upgrade or repair a live/old installation. Follow docs/MAINTENANCE.md for a side-by-side upgrade; do not delete configuration or data.'
}
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
  & python -m venv (Join-Path $projectRoot '.venv')
  if ($LASTEXITCODE -ne 0) { throw 'Python 3.10+ is required.' }
}
# Do not equate failed status/authentication with proof that an old service is
# stopped. A pre-existing kernel environment belongs to maintenance as well.
$previous = & $python -I -c "import importlib.metadata as m; print(m.version('xiaojiu-brain') if list(m.distributions(name='xiaojiu-brain')) else '')"
if ($LASTEXITCODE -ne 0) { throw 'Cannot inspect the existing environment; no packages were changed.' }
if ($previous) { throw 'An installed kernel already exists. Preserve this environment and use docs/MAINTENANCE.md; this first-install entry never overwrites it.' }
$wheelDir = Join-Path $projectRoot 'wheels'
$bundled = @(Get-ChildItem -LiteralPath $wheelDir -Filter 'xiaojiu_brain-*.whl' -ErrorAction SilentlyContinue)
if ($bundled.Count -gt 1) { throw 'Multiple kernel wheels found; use an intact release package.' }
$installSource = $projectRoot
if ($bundled.Count -eq 1) { $installSource = $bundled[0].FullName }
$pipArgs = @('-m', 'pip', 'install', '--no-cache-dir')
$dependencyManifest = Join-Path $projectRoot 'compat\runtime-dependencies.json'
$bundledDependencies = $false
if (Test-Path -LiteralPath $dependencyManifest) {
  $dependencies = Get-Content -LiteralPath $dependencyManifest -Raw -Encoding UTF8 | ConvertFrom-Json
  if ($dependencies.format -ne 1 -or @($dependencies.files).Count -ne 1) { throw 'Invalid runtime dependency manifest.' }
  foreach ($dependency in $dependencies.files) {
    if ($dependency.filename -ne 'tzdata-2026.6-py2.py3-none-any.whl') { throw 'Unknown bundled runtime dependency.' }
    $dependencyFile = Join-Path $wheelDir $dependency.filename
    if (-not (Test-Path -LiteralPath $dependencyFile) -or (Get-FileHash -LiteralPath $dependencyFile -Algorithm SHA256).Hash.ToLowerInvariant() -ne $dependency.sha256) {
      throw 'Bundled runtime dependency checksum mismatch.'
    }
  }
  $bundledDependencies = $true
}
if ($Offline -or ($bundled.Count -eq 1 -and $bundledDependencies)) { $pipArgs += @('--no-index', '--find-links', $wheelDir) }
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
