# xz-copilot-hub 启动脚本（PowerShell 7+）
# 用法：在项目根目录执行 .\xzstart.ps1
$ErrorActionPreference = 'Stop'

# 使用当前目录作为工作目录，避免硬编码旧路径
$WorkDir = (Get-Location).Path
Set-Location -Path $WorkDir
Write-Host "Working directory: $WorkDir" -ForegroundColor DarkGray

function Write-Step($msg) {
    Write-Host ""
    Write-Host "==> $msg" -ForegroundColor Cyan
}

# 1. 选择 Python 解释器（优先 venv）
$PythonExe = $null
if (Test-Path ".\.venv\Scripts\python.exe") {
    $PythonExe = ".\.venv\Scripts\python.exe"
    Write-Step "Using existing .venv interpreter"
} elseif (Test-Path ".\venv\Scripts\python.exe") {
    $PythonExe = ".\venv\Scripts\python.exe"
    Write-Step "Using existing venv interpreter"
} else {
    Write-Step "Creating new .venv ..."
    python -m venv .venv
    $PythonExe = ".\.venv\Scripts\python.exe"
}

# 2. 确保 pip 可用
& $PythonExe -m ensurepip --upgrade | Out-Null

# 3. 安装/更新依赖
Write-Step "Installing dependencies ..."
& $PythonExe -m pip install --upgrade pip | Out-Null
& $PythonExe -m pip install -r requirements.txt

if ($LASTEXITCODE -ne 0) {
    Write-Error "依赖安装失败"
    exit 1
}

# 4. 加载 .env（如存在）
if (Test-Path ".\.env") {
    Write-Step "Loading .env ..."
    Get-Content ".\.env" | ForEach-Object {
        $line = $_.Trim()
        if ($line -and -not $line.StartsWith("#")) {
            $parts = $line -split '=', 2
            if ($parts.Length -eq 2) {
                [System.Environment]::SetEnvironmentVariable($parts[0].Trim(), $parts[1].Trim(), "Process")
            }
        }
    }
}

# 5. 必要的环境变量提示
if (-not $env:CODEBUDDY_PASSWORD) {
    Write-Warning "CODEBUDDY_PASSWORD 未设置，可在 .env 中配置后再启动"
}

# 6. 可选：跑一遍测试
$runTests = $env:XZ_RUN_TESTS -eq "1"
if ($runTests) {
    Write-Step "Running tests ..."
    & $PythonExe -m pytest tests/ -v
    if ($LASTEXITCODE -ne 0) {
        Write-Error "测试未通过，仍将继续启动"
    }
}

# 7. 启动服务
Write-Step "Starting xz-copilot-hub service ..."
& $PythonExe web.py
