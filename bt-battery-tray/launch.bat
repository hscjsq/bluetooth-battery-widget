@echo off
rem Launch the installed standalone widget (no Python needed).
rem Falls back to the source version only if the installed exe is missing.
setlocal
set "EXE=%LOCALAPPDATA%\BluetoothBatteryWidget\BtBatteryWidget.exe"
if exist "%EXE%" (
  start "" "%EXE%"
  exit /b 0
)
rem ---- fallback: source version, needs WorkBuddy embedded Python ----
set "HERE=%~dp0"
set "PY="
if exist "%HERE%python_path.txt" (
  for /f "usebackq delims=" %%p in ("%HERE%python_path.txt") do (
    if exist "%%p" set "PY=%%p"
  )
)
if not defined PY (
  for /d %%d in ("%USERPROFILE%\.workbuddy\binaries\python\versions\*") do (
    if exist "%%d\pythonw.exe" set "PY=%%d\pythonw.exe"
  )
)
if not defined PY set "PY=pythonw.exe"
start "" "%PY%" "%HERE%bt_widget.py"
