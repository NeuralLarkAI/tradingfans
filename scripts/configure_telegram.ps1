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

function SecureStringToPlain([Security.SecureString]$s) {
  $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($s)
  try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) }
  finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
}

$envFile = Join-Path $RepoRoot '.env'
if (!(Test-Path $envFile)) {
  New-Item -ItemType File -Path $envFile -Force | Out-Null
}

$tokenSecure = Read-Host -Prompt "Paste TELEGRAM_BOT_TOKEN" -AsSecureString
$token = SecureStringToPlain $tokenSecure
if ([string]::IsNullOrWhiteSpace($token)) { throw "Empty token" }

$pinSecure = Read-Host -Prompt "Optional TELEGRAM_PAIR_PIN (press Enter to skip)" -AsSecureString
$pin = SecureStringToPlain $pinSecure

$lines = @()
if (Test-Path $envFile) { $lines = Get-Content $envFile }

$lines = Upsert-EnvLine $lines 'TELEGRAM_BOT_TOKEN' $token
if (-not [string]::IsNullOrWhiteSpace($pin)) {
  $lines = Upsert-EnvLine $lines 'TELEGRAM_PAIR_PIN' $pin
}

Set-Content -Path $envFile -Value $lines -Encoding utf8
Write-Host "Saved Telegram settings to $envFile"
Write-Host "Next: restart agent, then message the bot: /pair"

