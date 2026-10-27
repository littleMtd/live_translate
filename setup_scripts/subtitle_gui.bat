@echo off
rem Opens the subtitle window (no console). Double-click, or pin a shortcut to this file.
set "DIR=%~dp0.."
if not exist "%DIR%\live-subtitle-env\Scripts\pythonw.exe" (
    echo [ERROR] Virtual environment not found.
    pause
    exit /b 1
)
start "" "%DIR%\live-subtitle-env\Scripts\pythonw.exe" "%DIR%\scripts\subtitle_gui.py"
