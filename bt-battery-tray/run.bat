@echo off
chcp 65001 >nul
setlocal
set "HERE=%~dp0"
set "PY="
if exist "%HERE%python_path.txt" (
  for /f "usebackq delims=" %%p in ("%HERE%python_path.txt") do set "PY=%%p"
)
if not defined PY set "PY=python.exe"
set "PY=%PY:pythonw.exe=python.exe%"
echo Python: %PY%
echo.
"%PY%" "%HERE%bt_widget.py" --selftest %*
echo.
echo (Use start.vbs for the real tray app, no console.)
pause
