"""Canonical PromptBox launcher kept in the product directory."""
import importlib.util
import os
import subprocess
import sys
import traceback
from pathlib import Path


def resolve_base_dir() -> Path:
    """Resolve bundled files from the executable, not the caller's cwd."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


LOG_PATH = resolve_base_dir() / "promptbox_runtime.log"


def log(message: str) -> None:
    try:
        with LOG_PATH.open("a", encoding="utf-8") as file:
            file.write(message.rstrip() + "\n")
    except OSError:
        pass


def resolve_resource_dir() -> Path:
    """Resolve bundled Python and image resources for one-file builds."""
    bundle_dir = getattr(sys, "_MEIPASS", None)
    if bundle_dir:
        return Path(bundle_dir).resolve()
    return resolve_base_dir()


def desktop_path() -> Path:
    """Return the current user's Desktop directory without hard-coding its name."""
    configured = os.environ.get("USERPROFILE")
    if configured:
        return Path(configured) / "Desktop"
    return Path.home() / "Desktop"


def create_desktop_shortcut() -> bool:
    """Create the user-facing shortcut once when running as a bundled EXE."""
    if not getattr(sys, "frozen", False) or sys.platform != "win32":
        return False
    shortcut = desktop_path() / "PromptBox.lnk"
    if shortcut.exists():
        return False
    target = Path(sys.executable).resolve()
    shortcut.parent.mkdir(parents=True, exist_ok=True)
    escaped_target = str(target).replace("'", "''")
    escaped_shortcut = str(shortcut).replace("'", "''")
    script = (
        "$w=New-Object -ComObject WScript.Shell;"
        f"$s=$w.CreateShortcut('{escaped_shortcut}');"
        f"$s.TargetPath='{escaped_target}';"
        f"$s.WorkingDirectory='{str(target.parent).replace(chr(39), chr(39) * 2)}';"
        f"$s.IconLocation='{escaped_target},0';"
        "$s.Save()"
    )
    try:
        subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
            check=True,
            capture_output=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.CalledProcessError):
        log("[launcher] shortcut creation failed")
        log(traceback.format_exc())
        return False
    return True


def load_promptbox_module():
    if getattr(sys, "frozen", False):
        return importlib.import_module("promptbox")
    module_path = resolve_resource_dir() / "promptbox.py"
    base_dir = str(resolve_resource_dir())
    if base_dir not in sys.path:
        sys.path.insert(0, base_dir)
    spec = importlib.util.spec_from_file_location("promptbox_module", module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load PromptBox from {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> None:
    log("[launcher] starting PromptBox")
    try:
        create_desktop_shortcut()
        load_promptbox_module().main()
    except Exception:
        log("[launcher] fatal exception")
        log(traceback.format_exc())
        raise
    finally:
        log("[launcher] PromptBox exited")


if __name__ == "__main__":
    main()
