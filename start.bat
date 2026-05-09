@echo off
setlocal

set "DIR=%~dp0"
set "PYTHON=%DIR%live-subtitle-env\Scripts\python.exe"

if not exist "%PYTHON%" (
    echo [ERROR] Virtual environment not found.
    echo.
    echo Run the following to set up:
    echo   python -m venv live-subtitle-env
    echo   live-subtitle-env\Scripts\pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)

if not exist "%DIR%.env" (
    echo [WARN] .env not found -- API keys may be missing.
    echo Copy .env.example to .env and fill in your keys.
    echo.
)

cd /d "%DIR%"
"%PYTHON%" main.py %*

if %errorlevel% neq 0 (
    echo.
    echo [ERROR] Exited with code %errorlevel%
    pause
)
