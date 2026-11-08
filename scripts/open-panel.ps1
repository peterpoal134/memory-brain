$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$configPath = Join-Path $projectRoot 'private\config.json'
$logDirectory = Join-Path $projectRoot 'private\live\data\logs'
$logPath = Join-Path $logDirectory 'panel-launch.log'
$phase = 'configuration'
function Write-PanelLaunch([string]$message) {
  New-Item -ItemType Directory -Force -Path $logDirectory | Out-Null
  ('[{0}] {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $message) | Add-Content -LiteralPath $logPath -Encoding UTF8
}
function Request-PanelTicket($settings) {
  if ($settings.host -notin @('127.0.0.1', 'localhost', '::1')) { throw 'Loopback host required.' }
  $serverHost = [string]$settings.host
  if ($serverHost -eq '::1') { $serverHost = '[::1]' }
  $baseUrl = 'http://{0}:{1}' -f $serverHost, [int]$settings.port
  $request = [Net.HttpWebRequest]::Create($baseUrl + '/v1/session/ticket')
  $request.Proxy = $null
  $request.Method = 'POST'
  $request.ContentType = 'application/json'
  $request.Timeout = 10000
  $request.Headers.Add('Authorization', 'Bearer ' + [string]$settings.api_token)
  $body = [Text.Encoding]::UTF8.GetBytes('{}')
  $request.ContentLength = $body.Length
  $stream = $request.GetRequestStream()
  try { $stream.Write($body, 0, $body.Length) } finally { $stream.Close() }
  $response = $request.GetResponse()
  try {
    $reader = New-Object IO.StreamReader($response.GetResponseStream(), [Text.Encoding]::UTF8)
    try { $result = $reader.ReadToEnd() | ConvertFrom-Json } finally { $reader.Dispose() }
  } finally { $response.Close() }
  if (-not $result.ok -or [string]$result.data.ticket -notmatch '^[A-Za-z0-9_-]{40,}$') { throw 'Invalid local login ticket.' }
  return ($baseUrl + '/brain#ticket=' + [string]$result.data.ticket)
}
function Get-RegisteredBrowser {
  if (-not ('XiaojiuPanelBrowser' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
using System.Text;
public static class XiaojiuPanelBrowser {
  [DllImport("Shlwapi.dll", CharSet = CharSet.Unicode)]
  static extern uint AssocQueryString(uint flags, uint kind, string association, string extra, StringBuilder output, ref uint length);
  public static string Executable() {
    uint length = 0;
    AssocQueryString(0, 2, "http", "open", null, ref length);
    if (length == 0 || length > 32768) return null;
    var output = new StringBuilder((int)length);
    return AssocQueryString(0, 2, "http", "open", output, ref length) == 0 ? output.ToString() : null;
  }
}
'@
  }
  $executable = [XiaojiuPanelBrowser]::Executable()
  if ($executable -and (Test-Path -LiteralPath $executable) -and ([IO.Path]::GetFileName($executable) -in @('chrome.exe', 'msedge.exe', 'firefox.exe'))) { return $executable }
  return $null
}
try {
  if (-not (Test-Path -LiteralPath $configPath)) { throw 'Local configuration is missing.' }
  $settings = Get-Content -LiteralPath $configPath -Raw -Encoding UTF8 | ConvertFrom-Json
  $phase = 'local-login'
  try { $url = Request-PanelTicket $settings } catch {
    Write-PanelLaunch 'Local service not reachable; trying its owned launcher.'
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File (Join-Path $PSScriptRoot 'start-local.ps1') -NoOpen
    if ($LASTEXITCODE -ne 0) { throw 'The local brain could not be started.' }
    $url = Request-PanelTicket $settings
  }
  Write-PanelLaunch 'One-use login ticket issued; no management credential is written to this log.'
  $phase = 'browser-dispatch'
  $browser = Get-RegisteredBrowser
  if ($browser) {
    $newWindow = if ([IO.Path]::GetFileName($browser) -eq 'firefox.exe') { '-new-window' } else { '--new-window' }
    $process = Start-Process -FilePath $browser -ArgumentList @($newWindow, $url) -WindowStyle Normal -PassThru
    Write-PanelLaunch ('Browser new-window request dispatched: browser={0}, pid={1}' -f [IO.Path]::GetFileName($browser), $process.Id)
  } else {
    Start-Process -FilePath $url -ErrorAction Stop | Out-Null
    Write-PanelLaunch 'URL dispatched through the registered Windows browser.'
  }
  Write-Output 'The brain panel has been requested in a browser window with automatic local login.'
  exit 0
} catch {
  Write-PanelLaunch ('FAILED phase={0}, type={1}' -f $phase, $_.Exception.GetType().FullName)
  Add-Type -AssemblyName PresentationFramework
  [Windows.MessageBox]::Show(("未能自动打开忆核大脑面板。`n检查环节：{0}`n诊断日志：{1}`n记忆数据未受影响。" -f $phase, $logPath), '忆核大脑', 'OK', 'Error') | Out-Null
  exit 1
}
