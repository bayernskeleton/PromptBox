Set WshShell = CreateObject("WScript.Shell")
scriptDir = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
desktopPath = WshShell.ExpandEnvironmentStrings("%USERPROFILE%\Desktop")
Set Shortcut = WshShell.CreateShortcut(desktopPath & "\PromptBox.lnk")
Shortcut.TargetPath = scriptDir & "\start.bat"
Shortcut.WorkingDirectory = scriptDir
Shortcut.WindowStyle = 7
Shortcut.IconLocation = scriptDir & "\logos\promptbox.ico,0"
Shortcut.Description = "PromptBox - 双击启动，Ctrl+Shift+Space 唤起"
Shortcut.Save
WScript.Echo "桌面快捷方式已创建：" & desktopPath & "\PromptBox.lnk"
