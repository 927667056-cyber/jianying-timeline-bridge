@echo off
setlocal
chcp 65001 >nul
set "APP_HOME=%~dp0"

if exist "%APP_HOME%runtime\pythonw.exe" (
  "%APP_HOME%runtime\pythonw.exe" "%APP_HOME%bridge_gui.py"
  exit /b %errorlevel%
)
if exist "%LOCALAPPDATA%\Programs\Python\Python312\pythonw.exe" (
  "%LOCALAPPDATA%\Programs\Python\Python312\pythonw.exe" "%APP_HOME%bridge_gui.py"
  exit /b %errorlevel%
)
if exist "%USERPROFILE%\miniconda3\pythonw.exe" (
  "%USERPROFILE%\miniconda3\pythonw.exe" "%APP_HOME%bridge_gui.py"
  exit /b %errorlevel%
)
where pyw >nul 2>nul
if not errorlevel 1 (
  pyw -3 "%APP_HOME%bridge_gui.py"
  exit /b %errorlevel%
)

echo [已安全停止] 找不到 Python 3.10 或更高版本。
echo 请安装 64 位 Python，或联系 Codex 检查本机运行环境。
pause
exit /b 2
