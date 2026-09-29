@echo off
rem ============================================================================
rem scripts/run.bat - Windows wrapper for the three-stage pipeline.
rem
rem Kept under scripts/ so all entry-point wrappers live next to other
rem operator tooling (migrate_catalog_v2.py / purge_qf_out.py / etc.).
rem The file moved from orchestration/ in 2026-09-22 with the
rem `simulation server -> gdr -> etl` migration; behaviour is unchanged
rem because the wrapper only forwards %* to `python -m orchestration`.
rem
rem Forwards every %* argument to:  uv run python -m orchestration %*
rem (or system python when uv is not installed).
rem
rem IMPORTANT: this file is kept ASCII-only. Windows cmd.exe locks its code
rem page when opening a .bat file, so non-ASCII comments in rem lines are
rem would be misdecoded by the active code page (e.g. 936 / GBK) and surface
rem as bogus "is not recognized as an internal or external command" errors.
rem Detailed usage notes (in Chinese) live in orchestration/README.md.
rem
rem Quick reference:
rem   run.bat start --all-tasks --detach
rem   run.bat start --tasks T001,T003
rem   run.bat start --tasks E001,E002,E003 --parallelism 3
rem   run.bat start --all-tasks --dry-run
rem   run.bat status
rem   run.bat stop --timeout 30
rem   run.bat replay
rem   run.bat replay --no-archive
rem
rem Subcommands and options:
rem   start [--detach|--foreground] [--dry-run] [--tasks T1,T2,... | --all-tasks]
rem         [--parallelism N] [--stay]
rem   status
rem   stop [--timeout SECONDS]
rem   replay [--no-archive]
rem
rem Global options (before the subcommand):
rem   --config PATH     YAML path; defaults to <repo>/config/config.yaml.
rem                     Override with the SIMCTL_CONFIG env var.
rem
rem Notes vs simulate_serve CLI:
rem   - run.bat does NOT recognise --include-offline. T052/T053 (offline_only)
rem     are submitted unconditionally; skip them via explicit --tasks lists.
rem   - simulate_serve filters offline_only by default; orchestration does not
rem     (see orchestration/producer_simulate.py).
rem ============================================================================

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
