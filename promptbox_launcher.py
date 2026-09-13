"""Canonical PromptBox launcher kept in the product directory."""
import importlib.util
import os
import shutil
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


def startup_folder_path() -> Path:
    """Return the current user's Startup folder without hard-coding a username."""
    configured = os.environ.get("USERPROFILE")
    profile = Path(configured) if configured else Path.home()
    return profile / "AppData" / "Roaming" / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def _create_shortcut(shortcut: Path, target: Path) -> bool:
    """Create or refresh a Windows shell shortcut without showing a console."""
    shortcut.parent.mkdir(parents=True, exist_ok=True)
    escaped_target = str(target).replace("'", "''")
    escaped_shortcut = str(shortcut).replace("'", "''")
    escaped_working_directory = str(target.parent).replace("'", "''")
    script = (
        "$w=New-Object -ComObject WScript.Shell;"
        f"$s=$w.CreateShortcut('{escaped_shortcut}');"
        f"$s.TargetPath='{escaped_target}';"
        f"$s.WorkingDirectory='{escaped_working_directory}';"
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
        log(f"[launcher] shortcut creation failed: {shortcut}")
        log(traceback.format_exc())
        return False
    return True


def create_desktop_shortcut() -> bool:
    """Create or refresh the user-facing shortcut for a bundled EXE."""
    if not getattr(sys, "frozen", False) or sys.platform != "win32":
        return False
    target = Path(sys.executable).resolve()
    return _create_shortcut(desktop_path() / "PromptBox.lnk", target)


def create_startup_shortcut() -> bool:
    """Create or refresh current-user login startup for a bundled EXE."""
    if not getattr(sys, "frozen", False) or sys.platform != "win32":
        return False
    target = Path(sys.executable).resolve()
    return _create_shortcut(startup_folder_path() / "PromptBox.lnk", target)


def _runtime_supports_promptbox(executable: Path) -> bool:
    """Check that a Python executable can import PromptBox's GUI/runtime dependencies."""
    probe = "import tkinter, keyboard, pyperclip, PIL"
    try:
        result = subprocess.run(
            [str(executable), "-c", probe],
            check=False,
            capture_output=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError:
        return False
    return result.returncode == 0


def _candidate_python_runtimes() -> list[Path]:
    """Return local Python candidates, preferring the current runtime."""
    candidates = [Path(sys.executable)]
    if os.name == "nt":
        candidates.extend(
            Path(path) for path in (
                os.environ.get("PROMPTBOX_PYTHON", ""),
                r"C:\Program Files\Python310\python.exe",
            ) if path
        )
        for command in ("python.exe", "py.exe"):
            found = shutil.which(command)
            if found:
                candidates.append(Path(found))
    unique = []
    seen = set()
    for candidate in candidates:
        resolved = candidate.expanduser().resolve()
        if resolved not in seen and resolved.exists():
            unique.append(resolved)
            seen.add(resolved)
    return unique


def find_usable_runtime(candidates=None) -> Path | None:
    """Find a Python runtime that can actually import PromptBox dependencies."""
    for candidate in candidates or _candidate_python_runtimes():
        if _runtime_supports_promptbox(Path(candidate)):
            return Path(candidate).resolve()
    return None


def _run_source_with_usable_runtime(runtime: Path) -> None:
    """Re-exec the canonical launcher with a GUI-capable Python runtime."""
    executable = runtime
    if os.name == "nt" and runtime.name.lower() == "python.exe":
        windowless = runtime.with_name("pythonw.exe")
        if windowless.exists():
            executable = windowless
    env = os.environ.copy()
    env["PROMPTBOX_LAUNCHER_REEXEC"] = "1"
    subprocess.run(
        [str(executable), str(Path(__file__).resolve())],
        check=True,
        cwd=str(resolve_base_dir()),
        env=env,
    )


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
        if not getattr(sys, "frozen", False) and not os.environ.get("PROMPTBOX_LAUNCHER_REEXEC"):
            runtime = find_usable_runtime()
            current = Path(sys.executable).resolve()
            if runtime is None:
                raise RuntimeError(
                    "没有找到可运行 PromptBox 的 Python 环境，请安装带 Tkinter 的 Python 并安装 requirements.txt。"
                )
            if runtime != current:
                log(f"[launcher] re-exec with runtime: {runtime}")
                _run_source_with_usable_runtime(runtime)
                return
        create_desktop_shortcut()
        create_startup_shortcut()
        load_promptbox_module().main()
    except Exception:
        log("[launcher] fatal exception")
        log(traceback.format_exc())
        raise
    finally:
        log("[launcher] PromptBox exited")


if __name__ == "__main__":
    main()
