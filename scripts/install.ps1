param([switch]$Offline)
$ErrorActionPreference = 'Stop'
# Use this PowerShell host's utility module even when launched from a different host.
Import-Module (Join-Path $PSHOME 'Modules\Microsoft.PowerShell.Utility\Microsoft.PowerShell.Utility.psd1') -ErrorAction Stop
$projectRoot = Split-Path -Parent $PSScriptRoot
& (Join-Path $PSScriptRoot 'verify-package.ps1') -Root $projectRoot -RequireManifest:$Offline
$env:PYTHONUTF8 = '1'
$env:TEMP = Join-Path $projectRoot 'tmp'
$env:TMP = $env:TEMP
New-Item -ItemType Directory -Force -Path $env:TEMP | Out-Null
try {
  $installLock = [IO.File]::Open((Join-Path $env:TEMP 'first-install.lock'), 'OpenOrCreate', 'ReadWrite', 'None')
} catch [System.IO.IOException] {
  throw 'Another installer is running in this folder, or the install lock is not writable.'
}
try {
  $config = Join-Path $projectRoot 'private\config.json'
  if ((Test-Path -LiteralPath $config) -or (Test-Path -LiteralPath (Join-Path $projectRoot 'data\brain.db'))) {
    throw 'Existing configuration or memory data preserved. Follow docs/MAINTENANCE.md for recovery or a side-by-side upgrade.'
  }
  $freePort = $null
  foreach ($candidatePort in 18880..18889) {
    $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, $candidatePort)
    $listener.ExclusiveAddressUse = $true
    try { $listener.Start(); $freePort = $candidatePort; break } catch [System.Net.Sockets.SocketException] {} finally { $listener.Stop() }
  }
  if ($null -eq $freePort) { throw 'No free candidate port in 18880..18889. Free one port and retry; no packages were installed.' }

  $releaseFile = Join-Path $projectRoot 'PUBLIC-RELEASE.json'
  $wheelDir = Join-Path $projectRoot 'wheels'
  $installSource = $projectRoot
  $expectedVersion = $null
  $wheelHash = $null
  if (Test-Path -LiteralPath $releaseFile) {
    $expectedVersion = (Get-Content -LiteralPath $releaseFile -Raw -Encoding UTF8 | ConvertFrom-Json).kernel_version
    if ($expectedVersion -notmatch '^\d+\.\d+\.\d+(?:(?:a|b|rc)\d+)?$') { throw 'Invalid kernel version in the release manifest.' }
    $installSource = Join-Path $wheelDir "xiaojiu_brain-$expectedVersion-py3-none-any.whl"
    if (-not (Test-Path -LiteralPath $installSource -PathType Leaf)) { throw 'The kernel wheel declared by the release manifest is missing.' }
    $wheelHash = (Get-FileHash -LiteralPath $installSource -Algorithm SHA256).Hash
  } elseif ($Offline) {
    throw 'Offline installation requires the complete release package with its version manifest.'
  }
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
  if ($wheelHash -and -not $bundledDependencies) { throw 'The release is missing its bundled runtime dependency manifest.' }

  $privateDirectory = Split-Path -Parent $config
  New-Item -ItemType Directory -Force -Path $privateDirectory | Out-Null
  $python = Join-Path $projectRoot '.venv\Scripts\python.exe'
  if (-not (Test-Path -LiteralPath $python)) {
    & python -I -X utf8 -c 'import sys; sys.exit(not ((3,10) <= sys.version_info[:2] < (4,0)))'
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.10 or newer is required on PATH.' }
    & python -I -X utf8 -m venv (Join-Path $projectRoot '.venv')
    if ($LASTEXITCODE -ne 0) { throw 'Could not create a Python virtual environment.' }
  }
  $previous = & $python -I -X utf8 -c "import importlib.metadata as m; print(m.version('xiaojiu-brain') if list(m.distributions(name='xiaojiu-brain')) else '')"
  if ($LASTEXITCODE -ne 0) { throw 'Cannot inspect the existing environment; no packages were changed.' }
  $pending = Join-Path $env:TEMP 'first-install.pending'
  $resume = $wheelHash -and ($previous -eq $expectedVersion) -and (Test-Path -LiteralPath $pending -PathType Leaf) -and
    ((Get-Content -LiteralPath $pending -Raw -Encoding ASCII).Trim() -eq $wheelHash)
  if ($previous -and -not $resume) {
    throw 'An installed kernel already exists. Preserve it and use docs/MAINTENANCE.md.'
  }

  if (-not $resume) {
    if ($wheelHash) { Set-Content -LiteralPath $pending -Value $wheelHash -Encoding ASCII }
    $pipArgs = @('-I', '-X', 'utf8', '-m', 'pip', '--isolated', 'install', '--disable-pip-version-check', '--no-input', '--no-cache-dir')
    if ($wheelHash) { $pipArgs += @('--no-index', '--find-links', $wheelDir, '--only-binary=:all:') }
    $pipArgs += $installSource
    & $python @pipArgs
    if ($LASTEXITCODE -ne 0) { throw 'Package installation failed. Retry with the same intact package after correcting the reported error.' }
  }
  & $python -I -X utf8 -m xiaojiu_brain --config $config init --data-dir '../data' --port $freePort
  if ($LASTEXITCODE -ne 0) { throw 'Configuration initialization failed. Retry with the same intact package after correcting the reported error.' }

  Remove-Item -LiteralPath $pending -ErrorAction SilentlyContinue
  $ignoreTemplate = Join-Path $projectRoot 'gitignore.template'
  if (-not (Test-Path -LiteralPath (Join-Path $projectRoot '.gitignore')) -and (Test-Path -LiteralPath $ignoreTemplate)) {
    Copy-Item -LiteralPath $ignoreTemplate -Destination (Join-Path $projectRoot '.gitignore')
  }
  Write-Host 'Installed with a fresh local configuration. No memories or model keys were imported.'
  Write-Host 'Open the brain launcher for the panel, or use the DSH connection launcher.'
} finally {
  $installLock.Dispose()
}
