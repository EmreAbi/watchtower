@echo off
setlocal
"%~dp0watchtower.exe" plugin link "%~dp0radio"
exit /b %errorlevel%
