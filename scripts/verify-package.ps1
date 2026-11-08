param([string]$Root = (Split-Path -Parent $PSScriptRoot))
$ErrorActionPreference = 'Stop'
$manifest = Join-Path $Root 'SHA256SUMS.json'
if (-not (Test-Path -LiteralPath $manifest)) { return }
$base = [IO.Path]::GetFullPath($Root).TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar
$items = Get-Content -Raw -Encoding UTF8 -LiteralPath $manifest | ConvertFrom-Json
foreach ($item in $items.PSObject.Properties) {
  $file = [IO.Path]::GetFullPath((Join-Path $Root $item.Name))
  if (-not $file.StartsWith($base, [StringComparison]::OrdinalIgnoreCase)) { throw 'Invalid release manifest path.' }
  if (-not (Test-Path -LiteralPath $file -PathType Leaf)) { throw ('Release file missing: ' + $item.Name) }
  if ((Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash.ToLowerInvariant() -ne [string]$item.Value) {
    throw ('Release checksum mismatch: ' + $item.Name)
  }
}
Write-Host 'Release file checksums verified. This detects file damage; it is not a digital signature.'
