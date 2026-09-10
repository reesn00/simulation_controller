@echo off
setlocal

REM Switch to the directory where this script is located
pushd "%~dp0"

echo Cleaning output directories...

if exist "output\refine_data" (
    rmdir /s /q "output\refine_data"
    echo   cleaned: output\refine_data
) else (
    echo   skip [not found]: output\refine_data
)

if exist "output\qf_out" (
    rmdir /s /q "output\qf_out"
    echo   cleaned: output\qf_out
) else (
    echo   skip [not found]: output\qf_out
)

if exist "output\agent_trajectory" (
    rmdir /s /q "output\agent_trajectory"
    echo   cleaned: output\agent_trajectory
) else (
    echo   skip [not found]: output\agent_trajectory
)

if exist "output\orchestration\dead" (
    rmdir /s /q "output\orchestration\dead"
    echo   cleaned: output\orchestration\dead
) else (
    echo   skip [not found]: output\orchestration\dead
)

if exist "output\orchestration\logs" (
    rmdir /s /q "output\orchestration\logs"
    echo   cleaned: output\orchestration\logs
) else (
    echo   skip [not found]: output\orchestration\logs
)

if exist "output\orchestration\orchestration.db" (
    del /f /q "output\orchestration\orchestration.db"
    echo   cleaned: output\orchestration\orchestration.db
) else (
    echo   skip [not found]: output\orchestration\orchestration.db
)

if exist "output\runs" (
    rmdir /s /q "output\runs"
    echo   cleaned: output\runs
) else (
    echo   skip [not found]: output\runs
)

echo Done.
popd
pause
