@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

:: pack_qwenjina_refined.bat
:: Collect all *_refined.qwenjina.txt from output\refine_data and zip them.
::
:: Usage:
::   scripts\pack_qwenjina_refined.bat             - default output to scripts\qwenjina_refined.zip
::   scripts\pack_qwenjina_refined.bat out.zip      - specify output filename (relative to SCRIPT_DIR)
::   scripts\pack_qwenjina_refined.bat D-path\x.zip  - specify absolute output path

:: --- locate repo root (parent of script dir) ---
set "SCRIPT_DIR=%~dp0"
pushd "%SCRIPT_DIR%.." >nul 2>&1
set "REPO_ROOT=%CD%"
popd >nul

set "SRC_DIR=%REPO_ROOT%\output\refine_data"
set "PATTERN=*_refined.qwenjina.txt"

:: --- validate source directory ---
if not exist "%SRC_DIR%\." (
    echo [ERROR] source directory not found: %SRC_DIR%
    exit /b 1
)

:: --- determine output path (default: scripts dir) ---
if "%~1"=="" (
    set "OUT_ZIP=%SCRIPT_DIR%qwenjina_refined.zip"
) else (
    set "TMP_ARG=%~1"
    :: check for drive letter (absolute path) by looking at char 2
    set "CH2=!TMP_ARG:~1,1!"
    if "!CH2!"==":" (
        set "OUT_ZIP=%~1"
    ) else (
        set "OUT_ZIP=%SCRIPT_DIR%%~1"
    )
)

:: --- count matching files ---
set /a FILE_COUNT=0
for %%f in ("%SRC_DIR%\%PATTERN%") do set /a FILE_COUNT+=1
if !FILE_COUNT! equ 0 (
    echo [WARN] no files matching "%PATTERN%" in %SRC_DIR%
    exit /b 1
)

echo [INFO] source : %SRC_DIR%
echo [INFO] pattern: %PATTERN%
echo [INFO] found  : !FILE_COUNT! file(s)
echo [INFO] output : !OUT_ZIP!

:: --- remove old archive if exists ---
if exist "!OUT_ZIP!" del /q "!OUT_ZIP!"

:: --- zip via PowerShell Compress-Archive ---
powershell -NoProfile -ExecutionPolicy Bypass -Command "$f = Get-ChildItem -Path '%SRC_DIR%\%PATTERN%' -File; if (-not $f) { Write-Host '[ERROR] no files found'; exit 1 }; Compress-Archive -Path $f.FullName -DestinationPath '!OUT_ZIP!' -Force; if ($?) { Write-Host ('[OK]   packed ' + $f.Count + ' file(s)') } else { Write-Host '[ERROR] zip failed'; exit 1 }"

set "PS_RC=%errorlevel%"
if not "%PS_RC%"=="0" (
    echo [ERROR] packaging failed
    exit /b %PS_RC%
)

endlocal
