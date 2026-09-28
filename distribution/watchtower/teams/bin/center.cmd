@echo off
setlocal
set "PYTHONHOME="
set "PYTHONPATH="
set "WATCHTOWER_TEAMS_PY="
for /f "delims=" %%P in ('py -3 -I -c "import sys; assert sys.version_info.major == 3 and sys.version_info.minor // 10; print(sys.executable)" 2^>nul') do set "WATCHTOWER_TEAMS_PY=%%P"
if defined WATCHTOWER_TEAMS_PY goto run
for /f "delims=" %%P in ('python -I -c "import sys; assert sys.version_info.major == 3 and sys.version_info.minor // 10; print(sys.executable)" 2^>nul') do set "WATCHTOWER_TEAMS_PY=%%P"
if defined WATCHTOWER_TEAMS_PY goto run
echo Watchtower Teams needs Python 3.10+ on PATH. 1>&2
exit /b 1
:run
"%WATCHTOWER_TEAMS_PY%" -I -B "%~dp0..\launcher.py" %*
exit /b %errorlevel%
