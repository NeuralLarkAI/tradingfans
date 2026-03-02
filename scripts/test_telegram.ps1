param(
  [string]$RepoRoot = 'C:\Users\New\tradingfans'
)

$ErrorActionPreference = 'Stop'

function Import-DotEnv([string]$Path) {
  if (!(Test-Path $Path)) { return }
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

$envFile = Join-Path $RepoRoot '.env'
Import-DotEnv $envFile

$token = $env:TELEGRAM_BOT_TOKEN
if (!$token) {
  Write-Host "TELEGRAM_BOT_TOKEN is NOT set."
  Write-Host "Add it to: $envFile"
  Write-Host "Or run: powershell -ExecutionPolicy Bypass -File C:\\Users\\New\\tradingfans\\scripts\\configure_telegram.ps1"
  Write-Host "Then restart the agent and message the bot: /pair"
  exit 1
}

try {
  $me = Invoke-RestMethod -TimeoutSec 15 -Method Get ("https://api.telegram.org/bot{0}/getMe" -f $token)
  if (-not $me.ok) { throw "getMe returned ok=false" }
  $u = $me.result.username
  $id = $me.result.id
  Write-Host ("Bot OK: @{0} (id={1})" -f $u, $id)
} catch {
  Write-Host "Telegram API call failed (getMe). Check token + network."
  exit 2
}

try {
  $updates = Invoke-RestMethod -TimeoutSec 15 -Method Get ("https://api.telegram.org/bot{0}/getUpdates?timeout=0" -f $token)
  if (-not $updates.ok) { throw "getUpdates returned ok=false" }
  $n = @($updates.result).Count
  Write-Host ("Updates available: {0}" -f $n)
  if ($n -gt 0) {
    $last = $updates.result[-1]
    $chatId = $last.message.chat.id
    $text = $last.message.text
    Write-Host ("Last chat_id seen: {0}" -f $chatId)
    Write-Host ("Last text: {0}" -f $text)
    Write-Host "If the agent is running, send /pair from that chat and it should reply."
  } else {
    Write-Host "No updates yet. Open Telegram, find your bot, and send: /pair"
  }
} catch {
  Write-Host "Telegram API call failed (getUpdates)."
  exit 3
}
