@echo off
rem orchestration/run.bat - Windows wrapper
rem
rem Usage:
rem   run.bat start [--detach|--dry-run]
rem   run.bat status
rem   run.bat stop
rem   run.bat replay [--batch N]
rem   run.bat --config PATH <subcmd> ...

setlocal

set "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%.."

where uv >nul 2>&1
if %ERRORLEVEL%==0 (
    set "PYTHON_RUNNER=uv run python"
) else (
    set "PYTHON_RUNNER=python"
)

%PYTHON_RUNNER% -m orchestration %*

endlocal
