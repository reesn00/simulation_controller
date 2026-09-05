@echo off
setlocal EnableExtensions

rem ============================================================
rem  GDR pipeline launcher
rem
rem  Input dir (fixed):  ..\output\agent_trajectory  (trajectory JSONL)
rem  Output dir (fixed): gdr\refine_data
rem
rem  Usage:
rem    run.bat                 Process all input files
rem    run.bat 3               Process at most 3 files (--max-files 3)
rem    run.bat 3 --workers 4   First numeric arg becomes --max-files,
rem                            remaining args are passed through
rem ============================================================

cd /d "%~dp0"

set "MAXFILES="
set "ARGS="

rem Treat the first argument as max-files if it is a number
set "FIRST=%~1"
echo(%FIRST%| findstr /r "^[0-9][0-9]*$" >nul && (
    set "MAXFILES=%FIRST%"
    shift
)

:collect
if "%~1"=="" goto :run
set "ARGS=%ARGS% %1"
shift
goto :collect

:run
echo [run.bat] input=..\output\agent_trajectory output=refine_data max-files=%MAXFILES% (empty=all)
if defined MAXFILES (
    uv run python -m pipeline.cli --batch-input-dir ..\output\agent_trajectory --batch-output-dir refine_data --max-files %MAXFILES%%ARGS%
) else (
    uv run python -m pipeline.cli --batch-input-dir ..\output\agent_trajectory --batch-output-dir refine_data%ARGS%
)

endlocal