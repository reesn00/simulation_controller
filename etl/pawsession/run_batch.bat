@echo off
REM QwenPaw session -> SFT ETL 分批执行（Windows wrapper）
REM 用法:
REM   run_batch.bat 49 10                 总数 49，每批 10
REM   run_batch.bat 49 10 20              跳过前 20 个后开始
REM   run_batch.bat 49 10 0 1 1           offset=0, shuffle=1, seed=1
REM   run_batch.bat 49 10 0 0 0 D:\in D:\out
REM
REM 环境变量:
REM   set ETL_INPUT=D:\data\sessions
REM   set ETL_OUTPUT=D:\data\out

setlocal EnableDelayedExpansion

set "TOTAL=%~1"
set "BATCH=%~2"
set "OFFSET=%~3"
set "SHUFFLE_FLAG=%~4"
set "SEED=%~5"

if "%TOTAL%"=="" set "TOTAL=0"
if "%BATCH%"==""  set "BATCH=10"
if "%OFFSET%"=="" set "OFFSET=0"
if "%SHUFFLE_FLAG%"=="" set "SHUFFLE_FLAG=0"
if "%SEED%"=="" set "SEED=0"

if "%TOTAL%"=="0" (
    echo [batch] 必须指定总数，例如: run_batch.bat 49 10
    exit /b 2
)

if "%ETL_INPUT%"==""  set "ETL_INPUT=%~dp0origindata"
if "%ETL_OUTPUT%"=="" set "ETL_OUTPUT=%~dp0output"

set "ARGS=--input %ETL_INPUT% --output %ETL_OUTPUT% --total %TOTAL% --batch %BATCH%"
if not "%OFFSET%"=="0" set "ARGS=!ARGS! --offset %OFFSET%"
if not "%SHUFFLE_FLAG%"=="0" set "ARGS=!ARGS! --shuffle --seed %SEED%"

echo [batch] ETL_INPUT =%ETL_INPUT%
echo [batch] ETL_OUTPUT=%ETL_OUTPUT%
echo [batch] args      =!ARGS!

python "%~dp0run_batch.py" !ARGS!
set "RC=%ERRORLEVEL%"
echo [batch] exit=%RC%
exit /b %RC%
