#!/usr/bin/env pwsh
<#
.SYNOPSIS
    CodeBuddy2API service starter (PowerShell version).
.DESCRIPTION
    Mirrors start.bat: sets up a uv-managed venv, installs deps,
    prompts for CODEBUDDY_PASSWORD if missing, and runs web.py.
#>

$ErrorActionPreference = 'Stop'

# 输出辅助函数（先于调用点声明）
function Write-Step($msg)  { Write-Host "==> $msg" -ForegroundColor Cyan }
function Write-Ok($msg)    { Write-Host "OK  $msg" -ForegroundColor Green }
function Write-Warn($msg)  { Write-Host "WARN $msg" -ForegroundColor Yellow }
function Write-Err($msg)   { Write-Host "ERR $msg" -ForegroundColor Red }

# 优先使用硬编码路径；若该路径不存在则回退到当前工作目录并提示。
$HardcodedWorkDir = 'D:\dev\lawpaddle\xz-copilot-hub'
if (Test-Path -LiteralPath $HardcodedWorkDir) {
    $WorkDir = $HardcodedWorkDir
} else {
    $WorkDir = (Get-Location).Path
    Write-Warn "Hardcoded WorkDir not found: $HardcodedWorkDir"
    Write-Warn "Falling back to current location: $WorkDir"
}
Set-Location -Path $WorkDir
Write-Host "Working directory: $WorkDir" -ForegroundColor DarkGray

# 1. Check uv
Write-Step "Checking uv installation..."
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Err "uv is not installed or not in PATH"
    Write-Host "Install it from https://docs.astral.sh/uv/getting-started/installation/"
    Read-Host "Press Enter to exit"
    exit 1
}
Write-Ok "uv found: $((Get-Command uv).Source)"

# 2. Create venv if missing (Python 3.10..<3.14)
if (-not (Test-Path ".venv")) {
    Write-Step "Creating virtual environment with uv (Python >=3.10,<3.14)..."
    uv venv --python-preference only-managed --python ">=3.10,<3.14"
    if ($LASTEXITCODE -ne 0) {
        Write-Err "Failed to create virtual environment"
        exit 1
    }
    Write-Ok "Virtual environment created"
} else {
    Write-Ok "Virtual environment already exists"
}

# 3. Activate venv
$activateScript = Join-Path ".venv" "Scripts/Activate.ps1"
if (-not (Test-Path $activateScript)) {
    Write-Err "Activation script not found: $activateScript"
    exit 1
}
& $activateScript
# Activate.ps1 doesn't set $LASTEXITCODE, check $env:VIRTUAL_ENV instead
if (-not $env:VIRTUAL_ENV) {
    Write-Err "Failed to activate virtual environment (VIRTUAL_ENV not set)"
    exit 1
}
Write-Ok "Virtual environment activated: $env:VIRTUAL_ENV"

# 4. Install dependencies
Write-Step "Installing dependencies..."
uv pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) {
    Write-Err "Failed to install dependencies"
    exit 1
}
Write-Ok "Dependencies installed"

# 5. Resolve CODEBUDDY_PASSWORD (env -> .env -> interactive prompt)
if (-not $env:CODEBUDDY_PASSWORD) {
    if (Test-Path ".env") {
        Write-Step "Loading configuration from .env file..."
        Get-Content ".env" | ForEach-Object {
            $line = $_.Trim()
            if ($line -and -not $line.StartsWith('#')) {
                $idx = $line.IndexOf('=')
                if ($idx -gt 0) {
                    $key = $line.Substring(0, $idx).Trim()
                    $val = $line.Substring($idx + 1).Trim().Trim('"', "'")
                    if ($key -eq "CODEBUDDY_PASSWORD" -and $val) {
                        $env:CODEBUDDY_PASSWORD = $val
                    }
                }
            }
        }
    }

    if (-not $env:CODEBUDDY_PASSWORD) {
        Write-Warn "CODEBUDDY_PASSWORD environment variable is not set"
        Write-Host "Please set it in .env file or as environment variable"
        $secure = Read-Host "Enter password for API access" -AsSecureString
        $env:CODEBUDDY_PASSWORD = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
            [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
        )
    } else {
        Write-Ok "Using password from .env file"
    }
} else {
    Write-Ok "Using existing CODEBUDDY_PASSWORD environment variable"
}

# 6. Activate venv before running
$activateScript = Join-Path $WorkDir ".venv/Scripts/Activate.ps1"
if (-not (Test-Path $activateScript)) {
    Write-Err "Activation script not found: $activateScript"
    exit 1
}
& $activateScript
Write-Ok "Virtual environment re-activated for service startup"

# 7. Start service
Write-Step "Starting xz-copilot-hub service..."
try {
    python web.py
} finally {
    Write-Host ""
    Read-Host "Press Enter to exit"
}
