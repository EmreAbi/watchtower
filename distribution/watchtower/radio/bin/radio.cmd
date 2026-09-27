@echo off
@rem Watchtower-private Radio CLI; no global shim or PATH registry changes.
call "%~dp0hook.cmd" cli %*
exit /b %errorlevel%
