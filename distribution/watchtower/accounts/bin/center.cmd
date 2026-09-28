@echo off
setlocal
set "PYTHONHOME="
set "PYTHONPATH="
set "WATCHTOWER_ACCOUNTS_PY="
@rem Resolve Python aliases to the actual interpreter before entering the UI.
for /f "delims=" %%P in ('py -3 -I -c "import sys; assert sys.version_info.major == 3 and sys.version_info.minor // 10; print(sys.executable)" 2^>nul') do set "WATCHTOWER_ACCOUNTS_PY=%%P"
if defined WATCHTOWER_ACCOUNTS_PY goto run
for /f "delims=" %%P in ('python -I -c "import sys; assert sys.version_info.major == 3 and sys.version_info.minor // 10; print(sys.executable)" 2^>nul') do set "WATCHTOWER_ACCOUNTS_PY=%%P"
if defined WATCHTOWER_ACCOUNTS_PY goto run
echo Watchtower Accounts needs Python 3.10+ on PATH. Run setup-accounts.cmd. 1>&2
exit /b 1
:run
"%WATCHTOWER_ACCOUNTS_PY%" -I -B "%~dp0..\launcher.py" %*
exit /b %errorlevel%
