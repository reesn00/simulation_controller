@echo off
rem orchestration/run.bat — Windows wrapper
rem
rem 用法：
rem   run.bat start [--detach|--dry-run]   启动 master + workers
rem   run.bat status                       打印队列/进程/dead 状态
rem   run.bat stop                         优雅停止
rem   run.bat replay [--batch N]           dead 任务重新入队
rem   run.bat --config PATH <subcmd> ...   指定 config.yaml
rem
rem 所有参数透传给 ``python -m orchestration``，由该入口负责实际语义。
rem 这里只做路径解析和 forward.

setlocal

set "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%..\.."

rem 优先用 uv run（与项目 README 一致）；若不可用则 fallback 到 python -m
where uv >nul 2>&1
if %ERRORLEVEL%==0 (
    set "PYTHON_RUNNER=uv run python"
) else (
    set "PYTHON_RUNNER=python"
)

%PYTHON_RUNNER% -m orchestration %*

endlocal
