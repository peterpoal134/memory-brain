param([string]$Archive)
$ErrorActionPreference = 'Stop'
$project = Split-Path -Parent $PSScriptRoot
$verify = Join-Path $project 'scripts/verify-package.ps1'
$tempBase = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar
$testRoot = Join-Path $tempBase ('memory-brain-checksum-test-' + [guid]::NewGuid().ToString('N'))
$fixture = Join-Path $testRoot 'fixture'
$manifest = Join-Path $fixture 'SHA256SUMS.json'
$releaseFile = Join-Path $fixture 'PUBLIC-RELEASE.json'
$fixtureInstaller = Join-Path $fixture 'scripts/install.ps1'

function Assert-Rejected([scriptblock]$Action, [string]$Expected) {
  try {
    & $Action
  } catch {
    if ($_.Exception.Message -match $Expected) { return }
    throw
  }
  throw ('Expected verification to stop with: ' + $Expected)
}

function Write-Manifest([System.Collections.IDictionary]$Hashes) {
  $Hashes | ConvertTo-Json | Set-Content -LiteralPath $manifest -Encoding UTF8
}

New-Item -ItemType Directory -Path $testRoot | Out-Null
try {
  foreach ($directory in @('scripts', 'compat', 'wheels')) {
    New-Item -ItemType Directory -Path (Join-Path $fixture $directory) -Force | Out-Null
  }
  Copy-Item -LiteralPath $verify -Destination (Join-Path $fixture 'scripts/verify-package.ps1')
  Copy-Item -LiteralPath (Join-Path $project 'scripts/install.ps1') -Destination $fixtureInstaller
  & $verify -Root $fixture
  Assert-Rejected { & $fixtureInstaller -Offline } 'checksum manifest is missing'

  $metadata = [ordered]@{
    kernel_version = '0.2.11'
    bundled_runtime_dependencies = @('tzdata-test-py3-none-any.whl')
    checksum_member_count = 7
  }
  $metadata | ConvertTo-Json | Set-Content -LiteralPath $releaseFile -Encoding UTF8
  Assert-Rejected { & $fixtureInstaller } 'checksum manifest is missing'
  foreach ($invalid in @('', '{}', '[]', '[{"file":"value"}]', 'null', '"text"')) {
    Set-Content -LiteralPath $manifest -Value $invalid -Encoding UTF8
    Assert-Rejected { & $fixtureInstaller } 'nonempty JSON object'
  }
  Set-Content -LiteralPath $manifest -Value '{"unfinished":' -Encoding UTF8
  Assert-Rejected { & $fixtureInstaller } 'contains invalid JSON'
  if (Test-Path -LiteralPath (Join-Path $fixture 'tmp')) { throw 'Installer wrote state before validating the manifest.' }

  $kernel = 'wheels/xiaojiu_brain-0.2.11-py3-none-any.whl'
  $dependency = 'wheels/tzdata-test-py3-none-any.whl'
  $license = 'LICENSE'
  foreach ($name in @($kernel, $dependency, 'compat/runtime-dependencies.json', $license)) {
    Set-Content -LiteralPath (Join-Path $fixture $name) -Value 'checksum test fixture' -Encoding UTF8
  }
  $expected = [ordered]@{}
  foreach ($name in @('PUBLIC-RELEASE.json', 'scripts/install.ps1', 'scripts/verify-package.ps1',
      'compat/runtime-dependencies.json', $kernel, $dependency, $license)) {
    $expected[$name] = (Get-FileHash -LiteralPath (Join-Path $fixture $name) -Algorithm SHA256).Hash.ToLowerInvariant()
  }
  Write-Manifest $expected
  & $verify -Root $fixture -RequireManifest

  foreach ($name in @('../outside.txt', '/absolute.txt', 'wheels//second.whl', 'wheels/../LICENSE',
      'LICENSE.', 'LICENSE:stream', 'wheels\dependency.whl')) {
    Write-Manifest ([ordered]@{ $name = '0' * 64 })
    Assert-Rejected { & $verify -Root $fixture } 'Invalid or duplicate release manifest path'
  }
  Write-Manifest ([ordered]@{ LICENSE = 'not-a-sha256' })
  Assert-Rejected { & $verify -Root $fixture } 'Invalid release checksum'
  Write-Manifest ([ordered]@{ LICENSE = '0' * 64 })
  Assert-Rejected { & $verify -Root $fixture } 'Release checksum mismatch'
  Write-Manifest ([ordered]@{ LICENSE = $expected[$license] })
  Assert-Rejected { & $verify -Root $fixture } 'does not cover PUBLIC-RELEASE.json'

  $expected.Remove($license)
  Write-Manifest $expected
  Assert-Rejected { & $verify -Root $fixture } 'inventory is incomplete'
  $metadata.Remove('checksum_member_count')
  $metadata | ConvertTo-Json | Set-Content -LiteralPath $releaseFile -Encoding UTF8
  $expected['PUBLIC-RELEASE.json'] = (Get-FileHash -LiteralPath $releaseFile -Algorithm SHA256).Hash.ToLowerInvariant()
  $expected.Remove($kernel)
  Write-Manifest $expected
  Assert-Rejected { & $verify -Root $fixture } 'does not cover wheels/xiaojiu_brain-0\.2\.11'
  Remove-Item -LiteralPath $manifest, $releaseFile
  Assert-Rejected { & $verify -Root $fixture } 'checksum manifest is missing'

  if ($Archive) {
    $extracted = Join-Path $testRoot 'extracted'
    Expand-Archive -LiteralPath $Archive -DestinationPath $extracted
    $roots = @(Get-ChildItem -LiteralPath $extracted -Directory)
    if ($roots.Count -ne 1) { throw 'Expected the archive to contain exactly one package root.' }
    & $verify -Root $roots[0].FullName -RequireManifest
    & (Join-Path $roots[0].FullName 'scripts/verify-package.ps1') -Root $roots[0].FullName
  }
  Write-Host 'Package verification regressions passed.'
} finally {
  $resolved = (Resolve-Path -LiteralPath $testRoot).ProviderPath
  if (-not $resolved.StartsWith($tempBase, [StringComparison]::OrdinalIgnoreCase) -or
      (Split-Path -Leaf $resolved) -notmatch '\Amemory-brain-checksum-test-[0-9a-f]{32}\z') {
    throw 'Refusing to clean up an unexpected test directory.'
  }
  Remove-Item -LiteralPath $resolved -Recurse -Force
}
