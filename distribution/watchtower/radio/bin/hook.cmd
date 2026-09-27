@echo off
setlocal
set "WATCHTOWER_RADIO_PY="
@rem WindowsApps aliases can activate Python outside the caller's Job. Probe
@rem them only for sys.executable, then launch the actual interpreter directly.
for /f "delims=" %%P in ('py -3 -c "import sys; assert sys.version_info.major == 3 and sys.version_info.minor // 10; print(sys.executable)" 2^>nul') do set "WATCHTOWER_RADIO_PY=%%P"
if defined WATCHTOWER_RADIO_PY goto run
for /f "delims=" %%P in ('python -c "import sys; assert sys.version_info.major == 3 and sys.version_info.minor // 10; print(sys.executable)" 2^>nul') do set "WATCHTOWER_RADIO_PY=%%P"
if defined WATCHTOWER_RADIO_PY goto run
echo Watchtower Radio needs Python 3.10+ on PATH. 1>&2
exit /b 1
:run
"%WATCHTOWER_RADIO_PY%" -B "%~dp0..\watchtower_radio.py" %*
exit /b %errorlevel%
