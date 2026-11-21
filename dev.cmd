@echo off
setlocal
cd /d "%~dp0"
"%~dp0tools\python\python.exe" "%~dp0run.py" --no-browser %*
if errorlevel 1 pause
