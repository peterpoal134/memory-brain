param([string]$Runtime, [string]$ProfilePatch, [switch]$Remove)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
$config = Join-Path $projectRoot 'private\config.json'
if (-not (Test-Path -LiteralPath $python) -or -not (Test-Path -LiteralPath $config)) {
  throw 'Run the package installer first.'
}
& node --version
if ($LASTEXITCODE -ne 0) { throw 'Node.js 22+ must be available on PATH.' }
$nodeMajor = & node -p 'process.versions.node.split(".")[0]'
if ($LASTEXITCODE -ne 0 -or [int]$nodeMajor -lt 22) { throw 'Node.js 22+ is required.' }
if (-not $Runtime) { $Runtime = (Read-Host 'DSH runtime folder (contains node_modules)').Trim().Trim('"') }
$Runtime = (Resolve-Path -LiteralPath $Runtime).Path
if (-not $ProfilePatch) { $ProfilePatch = (Read-Host 'Target DSH profile cordis.patch.yml full path').Trim().Trim('"') }
$ProfilePatch = (Resolve-Path -LiteralPath $ProfilePatch).Path
$arguments = @((Join-Path $PSScriptRoot 'register-dsh.mjs'), '--runtime', $Runtime,
  '--profile-patch', $ProfilePatch, '--package-root', $projectRoot)
if ($Remove) { $arguments += '--remove' }
Write-Host 'Preview only. The running DSH process and model route will not be changed.'
& node @arguments
if ($LASTEXITCODE -ne 0) { throw 'Compatibility or conflict check failed. No profile was changed.' }
$answer = Read-Host 'Apply the displayed single-plugin change with a byte backup? [y/N]'
if ($answer -notin @('y', 'Y', 'yes', 'YES')) { Write-Host 'Canceled. Nothing changed.'; exit 0 }
& node @arguments --apply
if ($LASTEXITCODE -ne 0) { throw 'Connection change failed. Review the reported backup; no data is deleted.' }
Write-Host 'Done. Restart this DSH profile normally when convenient. Other services were not restarted.'
Write-Host 'Unloading the plugin does not erase its local database or pending message queue.'
