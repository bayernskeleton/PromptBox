"""Compatibility entry point for old PromptBox shortcuts."""
import importlib.util
from pathlib import Path


def _load_launcher_main():
    launcher_path = Path(__file__).resolve().with_name("promptbox_launcher.py")
    spec = importlib.util.spec_from_file_location("promptbox_launcher", launcher_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load launcher from {launcher_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.main


def main() -> None:
    _load_launcher_main()()


if __name__ == "__main__":
    main()
