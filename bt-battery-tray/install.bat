@echo off
chcp 65001 >nul
setlocal
set "HERE=%~dp0"
echo Installing pystray + pillow into vendor\ ...
python -m pip install --disable-pip-version-check --target "%HERE%vendor" pystray pillow
if errorlevel 1 (
  echo.
  echo FAILED. Check that Python is on PATH and you have network access.
  pause
  exit /b 1
)
echo.
echo Done. Now double-click start.vbs
pause
