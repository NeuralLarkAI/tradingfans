param(
  [string]$RepoRoot = 'C:\Users\New\tradingfans'
)

$ErrorActionPreference = 'Stop'

function Upsert-EnvLine([string[]]$Lines, [string]$Key, [string]$Value) {
  $pattern = "^\s*{0}\s*=" -f [Regex]::Escape($Key)
  $out = @()
  $found = $false
  foreach ($l in $Lines) {
    if ($l -match $pattern) {
      $out += ("{0}={1}" -f $Key, $Value)
      $found = $true
    } else {
      $out += $l
    }
  }
  if (-not $found) { $out += ("{0}={1}" -f $Key, $Value) }
  return ,$out
}

$function:Remove-EnvLine = {
  param([string[]]$Lines, [string]$Key)
  $pattern = "^\s*{0}\s*=" -f [Regex]::Escape($Key)
  $out = @()
  foreach ($l in $Lines) {
    if ($l -match $pattern) { continue }
    $out += $l
  }
  return ,$out
}

$envFile = Join-Path $RepoRoot '.env'
if (!(Test-Path $envFile)) {
  New-Item -ItemType File -Path $envFile -Force | Out-Null
}

$token = $null
try { $token = (Get-Clipboard -Raw) } catch {}
if ($null -eq $token) { $token = '' }
$token = $token.Trim()

if ([string]::IsNullOrWhiteSpace($token)) {
  Write-Host "Copy your TELEGRAM_BOT_TOKEN to clipboard, then press Enter."
  Read-Host | Out-Null
  try { $token = (Get-Clipboard -Raw) } catch { $token = '' }
  if ($null -eq $token) { $token = '' }
  $token = $token.Trim()
}

if ([string]::IsNullOrWhiteSpace($token)) {
  Write-Host "Clipboard was empty. Enter token directly (will be visible in this terminal):"
  $token = (Read-Host -Prompt "TELEGRAM_BOT_TOKEN").Trim()
}

if ([string]::IsNullOrWhiteSpace($token)) { throw "Empty token" }

# Sanitize: remove invisible / non-ASCII characters that often get pasted.
$token = -join ($token.ToCharArray() | Where-Object { $_ -match '[0-9A-Za-z:_-]' })

if ($token -notmatch '^\d+:[A-Za-z0-9_-]{20,}$') {
  throw "Token format looks wrong after sanitization. Re-copy from BotFather and try again."
}

$pin = ''
Write-Host ""
Write-Host "Optional: TELEGRAM_PAIR_PIN hardens pairing so only someone with the PIN can authorize chat control."
Write-Host "If you don't want a PIN, just press Enter."
Write-Host "If you previously set a PIN by accident, pressing Enter here will REMOVE it."
try { Set-Clipboard -Value '' } catch {}
$pin = (Read-Host -Prompt "TELEGRAM_PAIR_PIN (optional)").Trim()
if (-not [string]::IsNullOrWhiteSpace($pin)) {
  $pin = -join ($pin.ToCharArray() | Where-Object { $_ -match '[0-9A-Za-z@#%+=:_-]' })
}

$lines = @()
if (Test-Path $envFile) { $lines = Get-Content $envFile }

$lines = Upsert-EnvLine $lines 'TELEGRAM_BOT_TOKEN' $token
if (-not [string]::IsNullOrWhiteSpace($pin)) {
  $lines = Upsert-EnvLine $lines 'TELEGRAM_PAIR_PIN' $pin
} else {
  $lines = & $function:Remove-EnvLine $lines 'TELEGRAM_PAIR_PIN'
}

Set-Content -Path $envFile -Value $lines -Encoding utf8
Write-Host "Saved Telegram settings to $envFile"
Write-Host "Next: restart agent, then message the bot: /pair"

try { Set-Clipboard -Value '' } catch {}
