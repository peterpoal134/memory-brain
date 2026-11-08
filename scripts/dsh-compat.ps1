param([string]$Runtime, [switch]$Rollback)
$ErrorActionPreference = 'Stop'
if (-not $Runtime) { $Runtime = (Read-Host 'DSH runtime folder (contains node_modules)').Trim().Trim('"') }
$Runtime = (Resolve-Path -LiteralPath $Runtime).Path
$scriptPath = Join-Path $PSScriptRoot 'dsh-request-hook.mjs'
& node $scriptPath --runtime $Runtime
if ($LASTEXITCODE -ne 0) { throw 'This runtime is not one of the explicitly supported builds; nothing was changed.' }
Write-Host 'Optional compatibility change only. The normal memory plugin does not apply it automatically.'
Write-Host 'Close every DSH process using this runtime first. No process will be stopped or restarted here.'
if ($Rollback) { $answer = Read-Host 'Type ROLLBACK after closing DSH to restore the owned original backup' }
else { $answer = Read-Host 'Type APPLY after closing DSH to apply the displayed narrow request-only hook' }
if ($Rollback -and $answer -eq 'ROLLBACK') { & node $scriptPath --runtime $Runtime --rollback --dsh-stopped }
elseif (-not $Rollback -and $answer -eq 'APPLY') { & node $scriptPath --runtime $Runtime --apply --dsh-stopped }
else { Write-Host 'No compatibility changes were made.'; exit 0 }
if ($LASTEXITCODE -ne 0) { throw 'The compatibility action was refused; see the reported reason and backup.' }
Write-Host 'Completed. Restart DSH normally when convenient, then run the connection launcher.'
