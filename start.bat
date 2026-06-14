@echo off
echo Starting CodeBuddy2API...

REM Check if uv is installed
uv --version >nul 2>&1
if errorlevel 1 (
    echo uv is not installed or not in PATH
    echo Install it from https://docs.astral.sh/uv/getting-started/installation/
    pause
    exit /b 1
)

REM Create virtual environment if not exists (Python <3.14)
if not exist ".venv" (
    echo Creating virtual environment with uv (Python ^<3.14^)...
    uv venv --python-preference only-managed --python ">=3.10,<3.14"
)

REM Activate virtual environment
call .venv\Scripts\activate.bat

REM Install dependencies
echo Installing dependencies...
uv pip install -r requirements.txt

REM Check environment variables and load from .env if exists
if not defined CODEBUDDY_PASSWORD (
    if exist ".env" (
        echo Loading configuration from .env file...
        for /f "tokens=1,2 delims==" %%a in (.env) do (
            if "%%a"=="CODEBUDDY_PASSWORD" set CODEBUDDY_PASSWORD=%%b
        )
    )
    if not defined CODEBUDDY_PASSWORD (
        echo WARNING: CODEBUDDY_PASSWORD environment variable is not set
        echo Please set it in .env file or as environment variable
        set /p CODEBUDDY_PASSWORD="Enter password for API access: "
    ) else (
        echo Using password from .env file
    )
)

REM Start service
echo Starting CodeBuddy2API service...
python web.py

pause