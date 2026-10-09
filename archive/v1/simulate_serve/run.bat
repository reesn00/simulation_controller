@echo off
setlocal EnableExtensions

rem ============================================================
rem  simulate_serve launcher
rem
rem  Usage:
rem    run.bat                Run all tasks
rem    run.bat 3              Run at most 3 tasks (--limit 3)
rem    run.bat 3 --verbose    First numeric arg becomes --limit,
rem                           the rest are passed through
rem    run.bat --verbose      Non-numeric first arg: pass all through
rem ============================================================

rem Script lives inside simulate_serve/, run from the project root
cd /d "%~dp0.."

set "LIMIT="
set "ARGS="

rem Treat the first argument as limit if it is a number
set "FIRST=%~1"
echo(%FIRST%| findstr /r "^[0-9][0-9]*$" >nul && (
    set "LIMIT=%FIRST%"
    shift
)

:collect
if "%~1"=="" goto :run
set "ARGS=%ARGS% %1"
shift
goto :collect

:run
if defined LIMIT (
    echo [run.bat] uv run python -m simulate_serve --limit %LIMIT%%ARGS%
    uv run python -m simulate_serve --limit %LIMIT%%ARGS%
) else (
    echo [run.bat] uv run python -m simulate_serve%ARGS%
    uv run python -m simulate_serve%ARGS%
)

endlocal
