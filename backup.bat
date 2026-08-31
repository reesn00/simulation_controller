@echo off
setlocal

REM Switch to the directory where this script is located
pushd "%~dp0"

REM Go up one level and create the archive there
REM Keep .git/ so the repository history is preserved
cd ..
tar -czvf simulation_controller.tar.gz --exclude="simulation_controller/simulation_controller.tar.gz" --exclude="simulation_controller/.venv" --exclude="simulation_controller/output" --exclude="simulation_controller/dist" --exclude="simulation_controller/build" --exclude="simulation_controller/.coverage" --exclude="simulation_controller/.pytest_cache" --exclude="simulation_controller/.pytest-tmp" --exclude="simulation_controller/.codegraph" --exclude="simulation_controller/**/__pycache__" --exclude="simulation_controller/**/*.py[cod]" --exclude="simulation_controller/*.egg-info" --exclude="simulation_controller/.uv-cache" simulation_controller

popd
pause
