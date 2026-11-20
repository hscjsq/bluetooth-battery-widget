' Bluetooth Battery Widget - silent launcher for the installed standalone exe
Option Explicit
Dim shell, fso, exe, cmd
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

exe = shell.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\BluetoothBatteryWidget\BtBatteryWidget.exe"
If fso.FileExists(exe) Then
    cmd = """" & exe & """"
    On Error Resume Next
    shell.Run cmd, 0, False
    If Err.Number <> 0 Then
        MsgBox "Cannot start the widget." & vbCrLf & Err.Description, 16, "Bluetooth Battery Widget"
    End If
Else
    MsgBox "Installed widget not found:" & vbCrLf & exe & vbCrLf & _
           "Use the desktop shortcut, or run install.bat in this folder.", 16, "Bluetooth Battery Widget"
End If
