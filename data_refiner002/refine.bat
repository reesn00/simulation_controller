@echo off
setlocal EnableExtensions

rem ============================================================
rem  data_refiner launcher
rem
rem  Usage:
rem    refine.bat                Process all files
rem    refine.bat 3              Process at most 3 files (--limit 3)
rem    refine.bat 3 --dry-run    First numeric arg becomes --limit,
rem                              the rest are passed through
rem    refine.bat --dry-run      Non-numeric first arg: pass all through
rem ============================================================

rem 回到仓库根目录, data_refiner 的默认相对路径以根目录为基准
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
set "PYTHONIOENCODING=utf-8"
if defined LIMIT (
    echo [refine.bat] uv run python -m data_refiner --limit %LIMIT%%ARGS%
    uv run python -m data_refiner --limit %LIMIT%%ARGS%
) else (
    echo [refine.bat] uv run python -m data_refiner%ARGS%
    uv run python -m data_refiner%ARGS%
)

endlocal
