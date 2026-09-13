Set WshShell = CreateObject("WScript.Shell")
scriptDir = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
pythonw = WshShell.ExpandEnvironmentStrings("%PROMPTBOX_PYTHONW%")
If pythonw = "%PROMPTBOX_PYTHONW%" Or pythonw = "" Then
    If CreateObject("Scripting.FileSystemObject").FileExists("C:\Program Files\Python310\pythonw.exe") Then
        pythonw = "C:\Program Files\Python310\pythonw.exe"
    Else
        pythonw = "pythonw.exe"
    End If
End If
launcher = scriptDir & "\promptbox_launcher.py"
WshShell.Run """" & pythonw & """ """ & launcher & """", 0, False
