import importlib.util
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
    assert "C:\\Users\\30276" not in silent_launcher


def test_batch_launcher_has_no_user_specific_runtime_path():
    batch_launcher = (Path(__file__).resolve().parents[1] / "start.bat").read_text(
        encoding="utf-8"
    )

    assert "promptbox_launcher.py" in batch_launcher
    assert "PROMPTBOX_PYTHONW" in batch_launcher
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


def test_first_run_does_not_recreate_existing_shortcut(monkeypatch, tmp_path):
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

    assert launcher.create_desktop_shortcut() is False
    assert calls == []


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

