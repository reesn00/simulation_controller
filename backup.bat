@echo off
setlocal

:: 返回项目根目录（脚本所在目录）
cd /d "%~dp0"

:: 切换到上级目录，打包当前项目文件夹
:: 保留 .git/ 以支持完整仓库恢复；排除环境、缓存、构建产物和运行时输出
cd ..
tar -czvf useramulation.tar.gz ^
  --exclude="useramulation/.venv" ^
  --exclude="useramulation/output" ^
  --exclude="useramulation/dist" ^
  --exclude="useramulation/build" ^
  --exclude="useramulation/.coverage" ^
  --exclude="useramulation/.pytest_cache" ^
  --exclude="useramulation/.pytest-tmp" ^
  --exclude="useramulation/__pycache__" ^
  --exclude="useramulation/**/__pycache__" ^
  --exclude="useramulation/*.py[cod]" ^
  --exclude="useramulation/*.egg-info" ^
  --exclude="useramulation/.uv-cache" ^
  useramulation

pause
