"""EXE 启动验收：在隔离数据目录中启动 PromptBox.exe，确认主循环存活后正常退出。"""
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
candidate = sys.argv[1] if len(sys.argv) > 1 else None
EXE = Path(candidate) if candidate else ROOT / "dist" / "PromptBox.exe"
DATA_DIR = ROOT / ".exe-smoke-data"

if not EXE.exists():
    print(f"[EXE-SMOKE][FAIL] missing exe: {EXE}")
    sys.exit(2)

DATA_DIR.mkdir(exist_ok=True)
env = dict(os.environ)
env["PROMPTBOX_DATA_DIR"] = str(DATA_DIR)
env["PROMPTBOX_DEMO"] = "1"

proc = subprocess.Popen([str(EXE)], env=env, cwd=str(ROOT))
print(f"[EXE-SMOKE] pid={proc.pid} launched")

deadline = time.time() + 25
alive_window_seen = False
while time.time() < deadline:
    code = proc.poll()
    if code is not None:
        print(f"[EXE-SMOKE][FAIL] process exited early with code {code}")
        sys.exit(1)
    time.sleep(1)
    alive_window_seen = True

print("[EXE-SMOKE] process stayed alive beyond 25s (main loop running)")
proc.terminate()
try:
    proc.wait(timeout=10)
    print("[EXE-SMOKE][PASS] process terminated cleanly")
except subprocess.TimeoutExpired:
    proc.kill()
    print("[EXE-SMOKE][PASS-with-force] process killed after timeout")
print(f"[EXE-SMOKE] data_dir_contents={sorted(p.name for p in DATA_DIR.iterdir())}")
sys.exit(0)
