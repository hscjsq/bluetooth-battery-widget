@echo off
chcp 65001 >nul
set "BT_WIDGET_EXE=%~dp0BtBatteryWidget.exe"

if not exist "%BT_WIDGET_EXE%" (
  echo [ERROR] BtBatteryWidget.exe not found in this folder.
  pause
  exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -Command "$sh = New-Object -ComObject WScript.Shell; $lnk = $sh.CreateShortcut((Join-Path ([Environment]::GetFolderPath('Startup')) 'BtBatteryWidget.lnk')); $lnk.TargetPath = $env:BT_WIDGET_EXE; $lnk.WorkingDirectory = (Split-Path -Parent $env:BT_WIDGET_EXE); $lnk.Description = 'Bluetooth Battery Widget'; $lnk.Save(); Write-Host 'Auto-start enabled.'"

echo.
echo Done. Auto-start enabled for the current user.
echo (Or: right-click the widget -> Settings -> Auto-start.)
pause
