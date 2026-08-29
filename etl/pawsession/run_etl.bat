@echo off
REM QwenPaw session -> SFT ETL (Windows wrapper)
REM 用法:
REM   run_etl.bat                          处理 origindata 下全部 session
REM   run_etl.bat 5                        只处理 5 个
REM   run_etl.bat 5 10                     跳过 10 个后处理 5 个
REM   run_etl.bat 5 0 1                    处理 5 个，按 seed=1 打乱顺序
REM   run_etl.bat 5 0 0 custom_in custom_out
REM
REM 也支持环境变量覆盖:
REM   set ETL_INPUT=D:\data\sessions
REM   set ETL_OUTPUT=D:\data\out

setlocal EnableDelayedExpansion

set "LIMIT=%~1"
set "OFFSET=%~2"
set "SHUFFLE_FLAG=%~3"
set "SEED=%~4"

if "%LIMIT%"=="" set "LIMIT=0"
if "%OFFSET%"=="" set "OFFSET=0"
if "%SHUFFLE_FLAG%"=="" set "SHUFFLE_FLAG=0"
if "%SEED%"=="" set "SEED=0"

if "%ETL_INPUT%"=="" set "ETL_INPUT=%~dp0origindata"
if "%ETL_OUTPUT%"=="" set "ETL_OUTPUT=%~dp0output"

set "ARGS=--input %ETL_INPUT% --output %ETL_OUTPUT%"

if not "%LIMIT%"=="0" set "ARGS=!ARGS! --limit %LIMIT%"
if not "%OFFSET%"=="0" set "ARGS=!ARGS! --offset %OFFSET%"
if not "%SHUFFLE_FLAG%"=="0" (
    set "ARGS=!ARGS! --shuffle --seed %SEED%"
)

echo [bat] ETL_INPUT =%ETL_INPUT%
echo [bat] ETL_OUTPUT=%ETL_OUTPUT%
echo [bat] args      =!ARGS!

python "%~dp0run_etl.py" !ARGS!
set "RC=%ERRORLEVEL%"
echo [bat] exit=%RC%
exit /b %RC%
