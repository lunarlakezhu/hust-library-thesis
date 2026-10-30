# hust-library-thesis bootstrap (Windows PowerShell)
# Usage: powershell -ExecutionPolicy Bypass -File setup.ps1
# ASCII only on purpose: Windows PowerShell 5.1 mis-decodes non-ASCII .ps1 files.

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

function Fail($msg) {
    Write-Host ""
    Write-Host "[FAIL] $msg" -ForegroundColor Red
    exit 1
}

Write-Host "=== hust-library-thesis setup ===" -ForegroundColor Cyan
Write-Host "install dir: $root"
Write-Host ""

# --- 1. Python ---------------------------------------------------------------
Write-Host "[1/4] checking python ..."
$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) { Fail "python not found in PATH. Install Python 3.12 and tick 'Add python.exe to PATH'." }
$ver = & python -c "import sys; print('%d.%d.%d' % sys.version_info[:3])" 2>$null
if ($LASTEXITCODE -ne 0) { Fail "python runs but is unusable." }
Write-Host "      python $ver  ($($py.Source))"
$ok = & python -c "import sys; print(1 if sys.version_info[:2] >= (3,10) else 0)" 2>$null
if ($ok -ne "1") { Fail "Python 3.10+ required (3.12 recommended). Found $ver" }

# --- 2. dependencies ---------------------------------------------------------
Write-Host "[2/4] installing dependencies (mcp / playwright / pymupdf) ..."
& python -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { Fail "pip install failed. Check network / proxy, or run 'python -m pip install -r requirements.txt' manually." }

# --- 3. Edge check -----------------------------------------------------------
Write-Host "[3/4] checking Microsoft Edge ..."
$edge = @(
    "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    "C:\Program Files\Microsoft\Edge\Application\msedge.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $edge) {
    Write-Host "      [WARN] Edge not found in the default locations." -ForegroundColor Yellow
    Write-Host "      The service launches channel='msedge'; install Edge before using search/download."
} else {
    Write-Host "      found: $edge"
}

# --- 4. self test + registration snippet ------------------------------------
Write-Host "[4/4] running self test (server handshake + API list) ..."
& python tests\test_client.py desc | Select-String -Pattern "^TOOLS:|^### " | ForEach-Object { Write-Host "      $_" }

$entry = [ordered]@{
    command    = $py.Source
    args       = @((Join-Path $root "server\hust_library_thesis_server.py"))
    enabled    = $true
    timeoutMs  = 600000
}
$snippet = @{ mcpServers = @{ "hust-library-thesis" = $entry } } | ConvertTo-Json -Depth 5
# Write without BOM: PowerShell 5.1's -Encoding UTF8 prepends one, which breaks strict JSON parsers.
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText((Join-Path $root "mcp_entry.json"), $snippet, $utf8NoBom)

Write-Host ""
Write-Host "=== setup finished ===" -ForegroundColor Green
Write-Host "Registration entry written to: $root\mcp_entry.json"
Write-Host "Copy its 'hust-library-thesis' block into your MCP client config, then restart the client:"
Write-Host "  Kimi Code : %USERPROFILE%\.kimi-code\mcp.json"
Write-Host "  ZCode     : %USERPROFILE%\.zcode\cli\config.json"
Write-Host ""
Write-Host "Next: log in once on this machine (institutional state), see 安装说明.md section 5."
Write-Host "  python tests\test_client.py access"
Write-Host "  python server\hust_library_thesis_server.py --login-daemon"
