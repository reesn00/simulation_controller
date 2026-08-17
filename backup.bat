@echo off
setlocal

REM Switch to the directory where this script is located
pushd "%~dp0"

REM Go up one level and create the archive there
REM Keep .git/ so the repository history is preserved
cd ..
tar -czvf useramulation.tar.gz --exclude="useramulation/useramulation.tar.gz" --exclude="useramulation/.venv" --exclude="useramulation/output" --exclude="useramulation/dist" --exclude="useramulation/build" --exclude="useramulation/.coverage" --exclude="useramulation/.pytest_cache" --exclude="useramulation/.pytest-tmp" --exclude="useramulation/**/__pycache__" --exclude="useramulation/**/*.py[cod]" --exclude="useramulation/*.egg-info" --exclude="useramulation/.uv-cache" useramulation

popd
pause
