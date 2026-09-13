"""Build the PromptBox Windows release into the product directory."""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
SPEC_PATH = PROJECT_DIR / "PromptBox.spec"
EXE_PATH = PROJECT_DIR / "dist" / "PromptBox.exe"


def _candidate_pyinstallers() -> list[Path]:
    candidates: list[Path] = []
    configured = os.environ.get("PROMPTBOX_PYINSTALLER")
    if configured:
        candidates.append(Path(configured))
    candidates.extend(
        [
            Path.home() / "AppData" / "Roaming" / "Python" / "Python310" / "Scripts" / "pyinstaller.exe",
            Path(sys.executable).with_name("pyinstaller.exe"),
        ]
    )
    found = shutil.which("pyinstaller")
    if found:
        candidates.append(Path(found))

    unique: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        resolved = candidate.expanduser().resolve()
        if resolved.exists() and resolved not in seen:
            unique.append(resolved)
            seen.add(resolved)
    return unique


def find_pyinstaller() -> Path:
    for candidate in _candidate_pyinstallers():
        return candidate
    raise FileNotFoundError(
        "找不到 PyInstaller。请安装 PyInstaller，或设置 PROMPTBOX_PYINSTALLER 指向 pyinstaller.exe。"
    )


def run_tests() -> None:
    env = os.environ.copy()
    project_text = str(PROJECT_DIR)
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = project_text if not existing else project_text + os.pathsep + existing
    subprocess.run(
        [sys.executable, "-m", "pytest", "tests", "-q"],
        cwd=PROJECT_DIR,
        env=env,
        check=True,
    )


def build_release(pyinstaller: Path) -> Path:
    if not SPEC_PATH.exists():
        raise FileNotFoundError(f"缺少打包配置：{SPEC_PATH}")
    subprocess.run(
        [str(pyinstaller), "--clean", "--noconfirm", str(SPEC_PATH)],
        cwd=PROJECT_DIR,
        check=True,
    )
    if not EXE_PATH.exists():
        raise FileNotFoundError(f"打包完成但没有找到 EXE：{EXE_PATH}")
    return EXE_PATH


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the PromptBox Windows EXE")
    parser.add_argument(
        "--skip-tests",
        action="store_true",
        help="Skip the pytest suite before packaging",
    )
    args = parser.parse_args()

    if not args.skip_tests:
        print("Running PromptBox tests...", flush=True)
        run_tests()
    pyinstaller = find_pyinstaller()
    print(f"Building with {pyinstaller}...", flush=True)
    exe_path = build_release(pyinstaller)
    print(f"Release ready: {exe_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
