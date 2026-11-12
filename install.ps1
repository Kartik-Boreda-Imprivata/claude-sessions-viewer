# Installs the `csr` command (Windows). Defaults to --tui unless the args
# already request a non-interactive mode (--tui/--detail/--compare).
$ErrorActionPreference = "Stop"

$repo = "Kartik-Boreda-Imprivata/claude-sessions-viewer"
$rawBase = "https://raw.githubusercontent.com/$repo/main"
$libDir = Join-Path $env:LOCALAPPDATA "claude-sessions-viewer"
$binDir = Join-Path $libDir "bin"

$pythonCmd = if (Get-Command python -ErrorAction SilentlyContinue) { "python" } `
    elseif (Get-Command py -ErrorAction SilentlyContinue) { "py" } `
    else { throw "python not found; install Python 3 first" }

New-Item -ItemType Directory -Force -Path $libDir, $binDir | Out-Null

$scriptPath = Join-Path $libDir "claude_session_report.py"
Invoke-WebRequest -Uri "$rawBase/claude_session_report.py" -OutFile $scriptPath

$wrapperPath = Join-Path $binDir "csr.cmd"
@"
@echo off
setlocal enabledelayedexpansion
set WANT_TUI=1
for %%A in (%*) do (
  if "%%~A"=="--tui" set WANT_TUI=0
  if "%%~A"=="--detail" set WANT_TUI=0
  if "%%~A"=="--compare" set WANT_TUI=0
)
if "!WANT_TUI!"=="1" (
  $pythonCmd "$scriptPath" %* --tui
) else (
  $pythonCmd "$scriptPath" %*
)
"@ | Set-Content -Path $wrapperPath -Encoding ASCII

$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if ($userPath -notlike "*$binDir*") {
    [Environment]::SetEnvironmentVariable("Path", "$userPath;$binDir", "User")
    Write-Host "Added $binDir to your user PATH. Restart your terminal for it to take effect."
}

Write-Host "Installed csr to $wrapperPath"
