# PromptBox Windows shortcut

PromptBox must start independently. Do not open WorkBuddy first.

1. Prefer a packaged release. After rebuilding, distribute the new `PromptBox.exe` (or a ZIP containing it) and have the user double-click that EXE once. On first launch it creates or refreshes both the desktop shortcut and the current-user Windows login shortcut. The user does not need to open a VBS file, install Python, or create a Startup shortcut manually.
2. A later EXE update does not require redoing the Windows shortcut flow. Replace the EXE at the same installation path and launch it once; the launcher refreshes the desktop and Startup shortcuts to the current EXE path. Existing user data remains in `~/.promptbox/` and is not replaced by rebuilding. To make future developer releases repeatable, run `python scripts/build_windows.py`; it runs the test suite first, then rebuilds `dist\\PromptBox.exe`. Use `--skip-tests` only when the same checks have already been run separately.
3. For source-mode development only, run `start_silent.vbs` or `start.bat` from the repository folder. The launcher checks the current Python runtime and automatically falls back to `C:\Program Files\Python310\pythonw.exe` when the current runtime has no Tkinter or PromptBox dependencies; set `PROMPTBOX_PYTHONW` only when using another runtime.
4. If creating a manual shortcut for source mode, set **Target** to:

   ```text
   %PROMPTBOX_PYTHONW% <PromptBox repository>\promptbox_launcher.py
   ```

5. Set **Start in** to the PromptBox repository folder and select **Minimized** under shortcut **Properties → Run**.

The canonical packaged entry point is `PromptBox.exe`; `promptbox_launcher.py` is the source-mode entry point, and `promptbox_start.py` remains only as a compatibility shim for old shortcuts. The process keeps the existing `Ctrl+Shift+Space` Win32 hotkey; no global text-input monitoring is installed.
