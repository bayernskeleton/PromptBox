Set WshShell = CreateObject("WScript.Shell")
scriptDir = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
pythonw = WshShell.ExpandEnvironmentStrings("%PROMPTBOX_PYTHONW%")
If pythonw = "%PROMPTBOX_PYTHONW%" Or pythonw = "" Then
    pythonw = "pythonw.exe"
End If
launcher = scriptDir & "\promptbox_launcher.py"
WshShell.Run """" & pythonw & """ """ & launcher & """", 0, False
