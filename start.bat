@echo off
:: Source-mode launcher. Packaged users should start PromptBox.exe instead.
cd /d "%~dp0"
if defined PROMPTBOX_PYTHONW (
    set "PYTHONW=%PROMPTBOX_PYTHONW%"
) else (
    set "PYTHONW=pythonw.exe"
)
"%PYTHONW%" "%~dp0promptbox_launcher.py"
exit /b %ERRORLEVEL%
