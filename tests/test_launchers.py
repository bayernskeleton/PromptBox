import importlib.util
import sys
from pathlib import Path


def _load_launcher():
    launcher_path = Path(__file__).resolve().parents[1] / "promptbox_launcher.py"
    spec = importlib.util.spec_from_file_location("launcher_under_test", launcher_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_resolve_base_dir_is_launcher_directory():
    launcher = _load_launcher()

    assert launcher.resolve_base_dir() == Path(launcher.__file__).resolve().parent


def test_load_promptbox_module_uses_repository_path(monkeypatch, tmp_path):
    launcher = _load_launcher()
    monkeypatch.chdir(tmp_path)

    module = launcher.load_promptbox_module()
    expected = launcher.resolve_base_dir() / "promptbox.py"

    assert Path(module.__file__).resolve() == expected.resolve()
    assert expected.parent == launcher.resolve_base_dir()


def test_silent_launcher_uses_canonical_launcher_and_managed_runtime():
    silent_launcher = (Path(__file__).resolve().parents[1] / "start_silent.vbs").read_text(
        encoding="utf-8"
    )

    assert "\\promptbox_launcher.py" in silent_launcher
    assert "PROMPTBOX_PYTHONW" in silent_launcher
    assert "C:\\Program Files\\Python310\\pythonw.exe" in silent_launcher
    assert "C:\\Users\\30276" not in silent_launcher


def test_batch_launcher_has_no_user_specific_runtime_path():
    batch_launcher = (Path(__file__).resolve().parents[1] / "start.bat").read_text(
        encoding="utf-8"
    )

    assert "promptbox_launcher.py" in batch_launcher
    assert "PROMPTBOX_PYTHONW" in batch_launcher
    assert "C:\\Program Files\\Python310\\pythonw.exe" in batch_launcher
    assert "C:\\Users\\30276" not in batch_launcher


def test_packaging_instructions_use_a_runtime_placeholder_not_a_user_path():
    instructions = (
        Path(__file__).resolve().parents[1] / "packaging" / "PromptBox.lnk.instructions.md"
    ).read_text(encoding="utf-8")

    assert "PROMPTBOX_PYTHONW" in instructions
    assert "C:\\Users\\30276" not in instructions


def test_launcher_uses_executable_directory_when_frozen(monkeypatch, tmp_path):
    launcher = _load_launcher()
    monkeypatch.setattr(launcher.sys, "frozen", True, raising=False)
    monkeypatch.setattr(launcher.sys, "executable", str(tmp_path / "PromptBox.exe"))

    assert launcher.resolve_base_dir() == tmp_path


def test_frozen_launcher_imports_packaged_promptbox_module(monkeypatch):
    launcher = _load_launcher()
    packaged = object()
    calls = []

    monkeypatch.setattr(launcher.sys, "frozen", True, raising=False)
    monkeypatch.setattr(launcher.importlib, "import_module", lambda name: calls.append(name) or packaged)

    assert launcher.load_promptbox_module() is packaged
    assert calls == ["promptbox"]


def test_first_run_creates_desktop_shortcut_for_frozen_executable(monkeypatch, tmp_path):
    launcher = _load_launcher()
    executable = tmp_path / "PromptBox.exe"
    executable.write_bytes(b"")
    desktop = tmp_path / "Desktop"
    desktop.mkdir()
    calls = []

    monkeypatch.setattr(launcher.sys, "frozen", True, raising=False)
    monkeypatch.setattr(launcher.sys, "executable", str(executable))
    monkeypatch.setattr(launcher, "desktop_path", lambda: desktop)
    monkeypatch.setattr(launcher.subprocess, "run", lambda *args, **kwargs: calls.append((args, kwargs)))

    created = launcher.create_desktop_shortcut()

    assert created is True
    assert calls
    command = calls[0][0][0][-1]
    assert str(desktop / "PromptBox.lnk") in command
    assert str(executable) in command


def test_updated_executable_refreshes_existing_desktop_shortcut(monkeypatch, tmp_path):
    launcher = _load_launcher()
    desktop = tmp_path / "Desktop"
    desktop.mkdir()
    shortcut = desktop / "PromptBox.lnk"
    shortcut.write_bytes(b"existing")
    calls = []

    monkeypatch.setattr(launcher.sys, "frozen", True, raising=False)
    monkeypatch.setattr(launcher.sys, "executable", str(tmp_path / "PromptBox.exe"))
    monkeypatch.setattr(launcher, "desktop_path", lambda: desktop)
    monkeypatch.setattr(launcher.subprocess, "run", lambda *args, **kwargs: calls.append(args))

    assert launcher.create_desktop_shortcut() is True
    assert calls


def test_startup_folder_path_uses_current_user_profile(monkeypatch, tmp_path):
    launcher = _load_launcher()
    profile = tmp_path / "profile"
    monkeypatch.setenv("USERPROFILE", str(profile))

    assert launcher.startup_folder_path() == profile / (
        "AppData/Roaming/Microsoft/Windows/Start Menu/Programs/Startup"
    )


def test_first_run_creates_startup_shortcut_for_frozen_executable(monkeypatch, tmp_path):
    launcher = _load_launcher()
    executable = tmp_path / "PromptBox.exe"
    executable.write_bytes(b"")
    startup = tmp_path / "Startup"
    startup.mkdir()
    calls = []

    monkeypatch.setattr(launcher.sys, "frozen", True, raising=False)
    monkeypatch.setattr(launcher.sys, "executable", str(executable))
    monkeypatch.setattr(launcher, "startup_folder_path", lambda: startup)
    monkeypatch.setattr(launcher.subprocess, "run", lambda *args, **kwargs: calls.append((args, kwargs)))

    created = launcher.create_startup_shortcut()

    assert created is True
    assert calls
    command = calls[0][0][0][-1]
    assert str(startup / "PromptBox.lnk") in command
    assert str(executable) in command


def test_updated_executable_refreshes_existing_startup_shortcut(monkeypatch, tmp_path):
    launcher = _load_launcher()
    startup = tmp_path / "Startup"
    startup.mkdir()
    shortcut = startup / "PromptBox.lnk"
    shortcut.write_bytes(b"existing")
    calls = []

    monkeypatch.setattr(launcher.sys, "frozen", True, raising=False)
    monkeypatch.setattr(launcher.sys, "executable", str(tmp_path / "PromptBox.exe"))
    monkeypatch.setattr(launcher, "startup_folder_path", lambda: startup)
    monkeypatch.setattr(launcher.subprocess, "run", lambda *args, **kwargs: calls.append(args))

    assert launcher.create_startup_shortcut() is True
    assert calls


def test_screenshot_helper_uses_current_interpreter_and_repository_paths():
    screenshot_helper = (Path(__file__).resolve().parents[1] / "tools" / "make_screenshots.py").read_text(
        encoding="utf-8"
    )

    assert "sys.executable" in screenshot_helper
    assert "C:\\Users\\30276" not in screenshot_helper


def test_icon_helper_uses_repository_relative_paths():
    icon_helper = (Path(__file__).resolve().parents[1] / "logos" / "make_icons.py").read_text(
        encoding="utf-8"
    )

    assert "Path(__file__).resolve().parents[1]" in icon_helper
    assert "C:\\Users\\30276" not in icon_helper


def test_launcher_selects_usable_python_runtime_when_current_runtime_lacks_tk(monkeypatch, tmp_path):
    launcher = _load_launcher()
    current = tmp_path / "managed" / "python.exe"
    fallback = tmp_path / "Python310" / "python.exe"
    current.parent.mkdir(parents=True)
    fallback.parent.mkdir(parents=True)
    current.write_bytes(b"")
    fallback.write_bytes(b"")

    monkeypatch.setattr(launcher.sys, "executable", str(current))
    monkeypatch.setattr(
        launcher,
        "_runtime_supports_promptbox",
        lambda executable: Path(executable).resolve() == fallback.resolve(),
    )

    assert launcher.find_usable_runtime([current, fallback]) == fallback.resolve()


def test_launcher_reexecs_with_usable_runtime(monkeypatch, tmp_path):
    launcher = _load_launcher()
    runtime = tmp_path / "Python310" / "python.exe"
    runtime.parent.mkdir(parents=True)
    runtime.write_bytes(b"")
    calls = []

    monkeypatch.setattr(launcher.sys, "frozen", False, raising=False)
    monkeypatch.setattr(launcher.sys, "executable", str(tmp_path / "managed.exe"))
    monkeypatch.delenv("PROMPTBOX_LAUNCHER_REEXEC", raising=False)
    monkeypatch.setattr(launcher, "find_usable_runtime", lambda: runtime)
    monkeypatch.setattr(launcher, "_run_source_with_usable_runtime", calls.append)
    monkeypatch.setattr(launcher, "create_desktop_shortcut", lambda: calls.append("desktop"))
    monkeypatch.setattr(launcher, "create_startup_shortcut", lambda: calls.append("startup"))
    monkeypatch.setattr(launcher, "load_promptbox_module", lambda: (_ for _ in ()).throw(AssertionError("must re-exec")))

    launcher.main()

    assert calls == [runtime]


def test_reexec_preserves_windowless_python_launcher(monkeypatch, tmp_path):
    launcher = _load_launcher()
    python = tmp_path / "Python310" / "python.exe"
    pythonw = python.with_name("pythonw.exe")
    python.parent.mkdir(parents=True)
    python.write_bytes(b"")
    pythonw.write_bytes(b"")
    calls = []

    monkeypatch.setattr(launcher.sys, "executable", str(tmp_path / "managed" / "pythonw.exe"))
    monkeypatch.setattr(launcher.subprocess, "run", lambda *args, **kwargs: calls.append((args, kwargs)))
    monkeypatch.setattr(launcher, "_runtime_supports_promptbox", lambda executable: True)

    launcher._run_source_with_usable_runtime(python)

    assert calls[0][0][0][0] == str(pythonw)
    assert calls[0][1]["env"]["PROMPTBOX_LAUNCHER_REEXEC"] == "1"


def test_compatibility_shim_delegates_to_launcher(monkeypatch, tmp_path):
    shim_path = Path(__file__).resolve().parents[1] / "promptbox_start.py"
    repository = shim_path.parent.resolve()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "sys.path",
        [
            entry
            for entry in __import__("sys").path
            if Path(entry or ".").resolve() != repository
        ],
    )
    spec = importlib.util.spec_from_file_location("shim_under_test", shim_path)
    shim = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(shim)
    calls = []
    monkeypatch.setattr(shim, "_load_launcher_main", lambda: lambda: calls.append("main"))

    shim.main()

    assert calls == ["main"]

