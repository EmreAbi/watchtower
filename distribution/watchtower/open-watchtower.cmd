@echo off
setlocal
pushd "%~dp0"
"%~dp0watchtower.exe" %*
set "watchtowerExit=%errorlevel%"
popd
exit /b %watchtowerExit%
