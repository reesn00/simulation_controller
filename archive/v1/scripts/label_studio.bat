@echo off
rem ============================================================================
rem scripts/label_studio.bat - push C3 trajectories + scorecard to Label Studio.
rem
rem Full flow in one shot (default):
rem   label_studio.bat                  init-project -> status -> upload
rem   label_studio.bat dry-run          init-project -> status -> upload --dry-run
rem   label_studio.bat init             init-project only (idempotent)
rem   label_studio.bat status           connectivity + config self-check
rem   label_studio.bat upload [args]    push; extra args forwarded, e.g.
rem                                      --task-id T007 / --min-score 0.5
rem                                      --complexity-tier hard / --force
rem                                      --no-scorecard / --batch-size 20
rem   label_studio.bat purge [args]     delete the LS project (needs --confirm)
rem
rem API key resolution order (first hit wins):
rem   1. env LABEL_STUDIO_API_KEY
rem   2. file config\label_studio_api_key.txt   (gitignored)
rem The key is never taken as a command argument - command lines land in shell
rem history and are visible in the process list.
rem
rem Getting the key:
rem   1. open http://127.0.0.1:8099/user/login    (first run: /user/signup)
rem   2. open http://127.0.0.1:8099/user/account
rem   3. copy the "Personal Access Token"
rem     NOTE: a PAT is a JWT *refresh* token. The client exchanges it at
rem     POST /api/token/refresh for a short-lived access token automatically -
rem     you do NOT need to exchange it by hand.
rem
rem Port is 8099, NOT 8088. 8088 serves the QwenPaw agent backend. Pointing
rem base_url at 8088 does not report "cannot connect" - it reports an auth
rem error, which sends you off to re-check a perfectly good API key.
rem
rem Dedup: Label Studio 1.23 does NOT dedupe. Task.inner_id is an integer
rem field, the bulk import endpoint drops it silently, and re-importing the
rem same value still creates a new task every time. Dedup therefore rides on
rem a local ledger at output\label_studio\push_index__<project_id>.jsonl
rem (session_id -> LS task id). Do not delete it - you will push duplicates.
rem `purge --confirm` deletes the LS project; the stale ledger file for the
rem old project id is harmless (a new project gets a new id).
rem
rem init-project and upload both PATCH the local label_config into the
rem project. LS stores whatever XML the project was created with, and the
rem validate endpoint checks the XML you hand it - not what LS has - so
rem without the sync you get a green init followed by an import 400.
rem
rem config/config.yaml -> label_studio.base_url must already be 8099.
rem
rem IMPORTANT: this file is kept ASCII-only. cmd.exe locks its code page when
rem opening a .bat file, so non-ASCII comments in rem lines get misdecoded by
rem the active code page (e.g. 936 / GBK) and surface as bogus
rem "is not recognized as an internal or external command" errors.
rem Chinese usage notes live in docs/observability-label-studio.md.
rem ============================================================================

setlocal enabledelayedexpansion

set "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%.."

rem --- console encoding --------------------------------------------------------
rem Label Studio output is JSON carrying Chinese text; without this it renders
rem as ???? and makes error messages unreadable.
chcp 65001 >nul 2>&1
set "PYTHONIOENCODING=utf-8"

rem --- python runner -----------------------------------------------------------
where uv >nul 2>&1
if %ERRORLEVEL%==0 (
    set "PYTHON_RUNNER=uv run python"
) else (
    set "PYTHON_RUNNER=python"
)

rem --- action name validation --------------------------------------------------
rem Deliberately BEFORE the key check: `help` has to work without a key, since
rem reading the usage is how you find out how to supply one. Same for an
rem unknown action - complaining "no API key" there hides the real mistake.
if "%~1"==""                     goto :checked
if /i "%~1"=="help"   goto :usage
if /i "%~1"=="-h"     goto :usage
if /i "%~1"=="--help" goto :usage
if /i "%~1"=="all"      goto :checked
if /i "%~1"=="dry-run"  goto :checked
if /i "%~1"=="init"     goto :checked
if /i "%~1"=="init-project" goto :checked
if /i "%~1"=="status"   goto :checked
if /i "%~1"=="upload"   goto :checked
if /i "%~1"=="purge"    goto :checked
echo [label_studio] Unknown action: %~1
echo.
call :usage
exit /b 2
:checked

rem --- API key -----------------------------------------------------------------
if defined LABEL_STUDIO_API_KEY goto :have_key
if exist "config\label_studio_api_key.txt" (
    for /f "usebackq tokens=* delims= " %%a in ("config\label_studio_api_key.txt") do set "LABEL_STUDIO_API_KEY=%%a"
)
if not defined LABEL_STUDIO_API_KEY goto :no_key
:have_key

rem --- dispatch ----------------------------------------------------------------
if "%~1"==""            goto :run_all
if /i "%~1"=="all"     goto :run_all
if /i "%~1"=="dry-run" goto :run_dryrun
if /i "%~1"=="init"    goto :run_init
if /i "%~1"=="init-project" goto :run_init
if /i "%~1"=="status"  goto :run_status
if /i "%~1"=="upload"  goto :run_upload
goto :run_purge

rem --- actions -----------------------------------------------------------------

rem init is the gate: upload resolves the project by title and refuses to run
rem without one, so failing here must stop the chain instead of dumping a
rem confusing "project not found" further down.
:run_all
echo [label_studio] === 1/3 init-project ===
call :run_init
if %ERRORLEVEL% neq 0 exit /b %ERRORLEVEL%
echo [label_studio] === 2/3 status ===
call :run_status
if %ERRORLEVEL% neq 0 exit /b %ERRORLEVEL%
echo [label_studio] === 3/3 upload ===
call :run_upload
exit /b %ERRORLEVEL%

:run_dryrun
echo [label_studio] === 1/3 init-project ===
call :run_init
if %ERRORLEVEL% neq 0 exit /b %ERRORLEVEL%
echo [label_studio] === 2/3 status ===
call :run_status
if %ERRORLEVEL% neq 0 exit /b %ERRORLEVEL%
echo [label_studio] === 3/3 upload --dry-run (nothing is pushed) ===
%PYTHON_RUNNER% -m label_studio upload --dry-run
exit /b %ERRORLEVEL%

:run_init
%PYTHON_RUNNER% -m label_studio init-project
exit /b %ERRORLEVEL%

:run_status
%PYTHON_RUNNER% -m label_studio status
exit /b %ERRORLEVEL%

rem SHIFT does not rewrite %* (batch gotcha) - it always keeps the ORIGINAL full
rem argument list. So `%*` here would forward the subcommand a second time and
rem argparse would reject "upload upload". The tail has to be collected by hand
rem with %1..%9. Inline rather than in a :collect subroutine because SHIFT inside
rem a CALLed label also shifts the caller's arguments.
:run_upload
shift
set "REST_ARGS="
:collect_upload_args
if "%~1"=="" goto :do_upload
set "REST_ARGS=!REST_ARGS! %1"
shift
goto :collect_upload_args
:do_upload
%PYTHON_RUNNER% -m label_studio upload !REST_ARGS!
exit /b %ERRORLEVEL%

:run_purge
shift
set "REST_ARGS="
:collect_purge_args
if "%~1"=="" goto :do_purge
set "REST_ARGS=!REST_ARGS! %1"
shift
goto :collect_purge_args
:do_purge
%PYTHON_RUNNER% -m label_studio purge !REST_ARGS!
exit /b %ERRORLEVEL%

rem --- failures / help ---------------------------------------------------------

:no_key
echo [label_studio] No Label Studio API key found.
echo.
echo   Do ONE of these:
echo     set LABEL_STUDIO_API_KEY=^<token^>
echo     - or write the token into config\label_studio_api_key.txt  ^(gitignored^)
echo.
echo   Get the token: http://127.0.0.1:8099/user/login  then  /user/account
echo   Do NOT pass it as a command argument - shell history keeps it.
echo.
exit /b 1

:usage
echo Usage: label_studio.bat [all^|dry-run^|init^|status^|upload^|purge] [args...]
echo.
echo   all       init-project -^> status -^> upload          ^(default^)
echo   dry-run   same, but upload only prints the plan
echo   init      create / reuse the LS project  ^(idempotent^)
echo   status    connectivity + config self-check
echo   upload    push C3 + scorecard; extra args are forwarded
echo   purge     delete the LS project  ^(destructive; needs --confirm^)
echo.
echo Upload filters, e.g.:
echo   label_studio.bat upload --task-id T007
echo   label_studio.bat upload --min-score 0.5
echo   label_studio.bat upload --complexity-tier hard --force
echo.
echo API key: env LABEL_STUDIO_API_KEY, or config\label_studio_api_key.txt
exit /b 0

endlocal
