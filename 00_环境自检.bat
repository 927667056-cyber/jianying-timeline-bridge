@echo off
setlocal
chcp 65001 >nul
set "APP_HOME=%~dp0"

if exist "%APP_HOME%runtime\python.exe" set "BRIDGE_PY=%APP_HOME%runtime\python.exe"
if not defined BRIDGE_PY if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "BRIDGE_PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not defined BRIDGE_PY if exist "%USERPROFILE%\miniconda3\python.exe" set "BRIDGE_PY=%USERPROFILE%\miniconda3\python.exe"
if not defined BRIDGE_PY (
  echo [已安全停止] 找不到 Python 3.10 或更高版本。
  pause
  exit /b 2
)

"%BRIDGE_PY%" "%APP_HOME%bridge_cli.py" doctor
set "BRIDGE_RESULT=%errorlevel%"
echo.
if "%BRIDGE_RESULT%"=="0" (
  echo 环境自检通过，可以使用。
) else (
  echo 环境自检未通过，请根据上方提示处理；工具没有写入剪映工程。
)
pause
exit /b %BRIDGE_RESULT%
