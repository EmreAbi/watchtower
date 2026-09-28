@echo off
setlocal
set "PYTHONHOME="
set "PYTHONPATH="
set "WATCHTOWER_ACCOUNTS_PY="
for /f "delims=" %%P in ('py -3 -I -c "import sys; assert sys.version_info.major == 3 and sys.version_info.minor // 10; print(sys.executable)" 2^>nul') do set "WATCHTOWER_ACCOUNTS_PY=%%P"
if defined WATCHTOWER_ACCOUNTS_PY goto run
for /f "delims=" %%P in ('python -I -c "import sys; assert sys.version_info.major == 3 and sys.version_info.minor // 10; print(sys.executable)" 2^>nul') do set "WATCHTOWER_ACCOUNTS_PY=%%P"
if defined WATCHTOWER_ACCOUNTS_PY goto run
echo Watchtower Accounts requires Python 3.10+ on PATH. 1>&2
exit /b 1
:run
"%WATCHTOWER_ACCOUNTS_PY%" -B "%~dp0accounts\setup.py"
if errorlevel 1 exit /b %errorlevel%
"%~dp0watchtower.exe" plugin link "%~dp0accounts"
if errorlevel 1 exit /b %errorlevel%
if exist "%~dp0results\herdr-plugin.toml" "%~dp0watchtower.exe" plugin link "%~dp0results"
if errorlevel 1 exit /b %errorlevel%
if exist "%~dp0teams\herdr-plugin.toml" "%~dp0watchtower.exe" plugin link "%~dp0teams"
if errorlevel 1 exit /b %errorlevel%
if exist "%~dp0benchmarks\herdr-plugin.toml" "%~dp0watchtower.exe" plugin link "%~dp0benchmarks"
exit /b %errorlevel%
