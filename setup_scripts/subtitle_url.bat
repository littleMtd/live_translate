@echo off
setlocal
chcp 65001 >nul

rem Korean video URL -> zh-TW subtitles. Double-click to be asked for the URL,
rem or: subtitle_url.bat <url> [--lyrics] [--out DIR] [--profile ID]

set "DIR=%~dp0.."
set "PYTHON=%DIR%\live-subtitle-env\Scripts\python.exe"
if not exist "%PYTHON%" (
    echo [ERROR] Virtual environment not found: %PYTHON%
    pause
    exit /b 1
)
cd /d "%DIR%"

if not "%~1"=="" (
    "%PYTHON%" scripts\subtitle_url.py %*
    goto :done
)

set "URL="
set /p "URL=影片網址: "
if "%URL%"=="" exit /b 0
set "LYRICS="
set /p "LYRICS=這是歌曲嗎？(y/N): "
if /i "%LYRICS%"=="y" (
    "%PYTHON%" scripts\subtitle_url.py "%URL%" --lyrics
) else (
    "%PYTHON%" scripts\subtitle_url.py "%URL%"
)

:done
if %errorlevel% neq 0 (
    echo.
    echo [ERROR] 失敗，錯誤碼 %errorlevel%
)
pause
