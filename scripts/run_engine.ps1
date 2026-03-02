param(
  [string]$RepoRoot = 'C:\Users\New\tradingfans',
  [string]$Symbol = 'both',
  [int]$Port = 7331,
  [bool]$DryRun = $true,
  [bool]$StopExisting = $true
)

$ErrorActionPreference = 'Stop'

function Import-DotEnv([string]$Path) {
  if (!(Test-Path $Path)) { throw "Missing .env at $Path" }
  Get-Content $Path | ForEach-Object {
    $line = $_.Trim()
    if ($line -eq '' -or $line.StartsWith('#')) { return }
    if ($line.StartsWith('export ')) { $line = $line.Substring(7).Trim() }
    if ($line -match '^([^=]+)=(.*)$') {
      $name = $matches[1].Trim()
      $val = $matches[2].Trim()
      if (
        ($val.StartsWith('"') -and $val.EndsWith('"')) -or
        ($val.StartsWith("'") -and $val.EndsWith("'"))
      ) {
        $val = $val.Substring(1, $val.Length - 2)
      }
      [Environment]::SetEnvironmentVariable($name, $val, 'Process')
    }
  }
}

$workDir = Join-Path $RepoRoot 'skills\TradingFans'
$envFile = Join-Path $RepoRoot '.env'
Import-DotEnv $envFile

if ($StopExisting) {
  $pids =
    Get-NetTCPConnection -LocalPort $Port -ErrorAction SilentlyContinue |
    Where-Object { $_.State -eq 'Listen' } |
    Select-Object -ExpandProperty OwningProcess -Unique

  foreach ($procId in $pids) {
    try { Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue } catch {}
  }
  if ($pids.Count -gt 0) { Start-Sleep -Seconds 1 }
}

Set-Location $workDir
$env:PYTHONPATH = 'src'
if ($DryRun) { $env:POLY_DRY_RUN = '1' } else { $env:POLY_DRY_RUN = '0' }

$py = if (Test-Path '.\.venv\Scripts\python.exe') { '.\.venv\Scripts\python.exe' } else { 'python' }
$args = @('-m', 'tradingfans.engine')
if ($DryRun) { $args += '--dry-run' }
$args += @('--symbol', $Symbol)

$p = Start-Process -FilePath $py -ArgumentList $args -PassThru -WindowStyle Hidden

$state = $null
for ($i = 0; $i -lt 15; $i++) {
  try {
    $state = Invoke-RestMethod -TimeoutSec 2 ("http://127.0.0.1:{0}/api/state" -f $Port)
    break
  } catch {
    Start-Sleep -Seconds 1
  }
}

if ($null -eq $state) {
  "TradingFans started (pid={0}) but dashboard not responding yet on port {1}" -f $p.Id, $Port
  throw "Dashboard did not respond on port $Port"
}

"TradingFans started (pid={0}) dry_run={1} scan_count={2} trade_count={3}" -f $p.Id, $state.dry_run, $state.scan_count, $state.trade_count
"Dashboard: http://127.0.0.1:{0}" -f $Port
