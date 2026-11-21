param([string]$Root = (Split-Path -Parent $PSScriptRoot), [switch]$RequireManifest)
$ErrorActionPreference = 'Stop'
# Use this PowerShell host's utility module even when launched from a different host.
Import-Module (Join-Path $PSHOME 'Modules\Microsoft.PowerShell.Utility\Microsoft.PowerShell.Utility.psd1') -ErrorAction Stop
$manifest = Join-Path $Root 'SHA256SUMS.json'
$releaseFile = Join-Path $Root 'PUBLIC-RELEASE.json'
if (-not (Test-Path -LiteralPath $manifest)) {
  $wheels = Join-Path $Root 'wheels'
  $bundledKernel = (Test-Path -LiteralPath $wheels -PathType Container) -and
    (Get-ChildItem -LiteralPath $wheels -File -Filter 'xiaojiu_brain-*.whl' | Select-Object -First 1)
  if ($RequireManifest -or (Test-Path -LiteralPath $releaseFile) -or $bundledKernel) {
    throw 'Release checksum manifest is missing. Extract the complete release package again.'
  }
  return
}
$base = [IO.Path]::GetFullPath($Root).TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar
$manifestText = Get-Content -Raw -Encoding UTF8 -LiteralPath $manifest
if ($manifestText -notmatch '^\s*\{') { throw 'Release checksum manifest must be a nonempty JSON object.' }
try {
  $manifestObject = ConvertFrom-Json -InputObject $manifestText
} catch {
  throw 'Release checksum manifest contains invalid JSON.'
}
$items = @($manifestObject.PSObject.Properties)
if ($items.Count -eq 0) { throw 'Release checksum manifest must be a nonempty JSON object.' }
$members = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
foreach ($item in $items) {
  $parts = $item.Name.Split('/')
  if ($item.Name -match '[\\:*?"<>|\p{Cc}]' -or ($parts -contains '') -or ($parts -match '[. ]$') -or
      -not $members.Add($item.Name)) { throw 'Invalid or duplicate release manifest path.' }
  if ($item.Value -isnot [string] -or $item.Value -notmatch '\A[0-9a-f]{64}\z') { throw 'Invalid release checksum.' }
  $file = [IO.Path]::GetFullPath((Join-Path $Root $item.Name))
  if (-not $file.StartsWith($base, [StringComparison]::OrdinalIgnoreCase)) { throw 'Invalid release manifest path.' }
  if (-not (Test-Path -LiteralPath $file -PathType Leaf)) { throw ('Release file missing: ' + $item.Name) }
  if ((Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash.ToLowerInvariant() -ne [string]$item.Value) {
    throw ('Release checksum mismatch: ' + $item.Name)
  }
}
if (-not $members.Contains('PUBLIC-RELEASE.json')) { throw 'Release manifest does not cover PUBLIC-RELEASE.json.' }
$release = Get-Content -Raw -Encoding UTF8 -LiteralPath $releaseFile | ConvertFrom-Json
if ($release.kernel_version -isnot [string] -or $release.kernel_version -notmatch '\A\d+\.\d+\.\d+(?:(?:a|b|rc)\d+)?\z') {
  throw 'Invalid kernel version in the release manifest.'
}
if ($release.PSObject.Properties['checksum_member_count']) {
  $memberCount = $release.checksum_member_count
  if (($memberCount -isnot [int] -and $memberCount -isnot [long]) -or $memberCount -ne $members.Count) {
    throw 'Release checksum manifest inventory is incomplete.'
  }
}
$requiredMembers = @('scripts/install.ps1', 'scripts/verify-package.ps1', 'compat/runtime-dependencies.json',
  "wheels/xiaojiu_brain-$($release.kernel_version)-py3-none-any.whl")
foreach ($dependency in @($release.bundled_runtime_dependencies)) {
  if ($dependency -isnot [string] -or $dependency -notmatch '\A[A-Za-z0-9_.-]+\.whl\z') {
    throw 'Invalid bundled runtime dependency in the release manifest.'
  }
  $requiredMembers += "wheels/$dependency"
}
foreach ($required in $requiredMembers) {
  if (-not $members.Contains($required)) { throw ('Release manifest does not cover ' + $required + '.') }
}
Write-Host 'Release file checksums verified. This detects file damage; it is not a digital signature.'
