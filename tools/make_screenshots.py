import os
import sys
import time
import subprocess
import ctypes
import ctypes.wintypes as wintypes

import win32gui
import win32ui
import win32con
from PIL import Image
import keyboard

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(REPO, "screenshots")
os.makedirs(OUT_DIR, exist_ok=True)

PYTHON = sys.executable
PROMPTBOX = os.path.join(REPO, "promptbox.py")


def find_promptbox_window():
    """Return (hwnd, (left,top,right,bottom)) for the first visible window with 'PromptBox' in title."""
    found = []
    all_titles = []

    def cb(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return True
        title = win32gui.GetWindowText(hwnd)
        if title:
            all_titles.append(title)
        if "PromptBox" in title:
            rect = win32gui.GetWindowRect(hwnd)
            if rect[2] - rect[0] > 200 and rect[3] - rect[1] > 200:
                found.append((hwnd, rect))
        return True

    win32gui.EnumWindows(cb, None)
    if not found:
        hits = [t for t in all_titles if "prompt" in t.lower()]
        if hits:
            print(f"[demo] debug: 'prompt' windows found: {hits}")
    return found[0] if found else (None, None)


def capture_window_clean(hwnd):
    """Capture only the window interior, no border/title-bar, no desktop clutter."""
    # Get client area (interior) in screen coordinates
    client_rect = win32gui.GetClientRect(hwnd)
    left_top = win32gui.ClientToScreen(hwnd, (0, 0))
    left = left_top[0]
    top = left_top[1]
    right = left + client_rect[2]
    bottom = top + client_rect[3]

    # Bring to foreground
    try:
        win32gui.SetForegroundWindow(hwnd)
    except Exception:
        pass
    time.sleep(0.4)

    from PIL import ImageGrab
    img = ImageGrab.grab(bbox=(left, top, right, bottom))
    return img


def main():
    env = os.environ.copy()
    env["PROMPTBOX_DEMO"] = "1"

    print("[demo] launching PromptBox in demo mode...")
    log_path = os.path.join(OUT_DIR, "_demo.log")
    log_fh = open(log_path, "w", encoding="utf-8")
    proc = subprocess.Popen(
        [PYTHON, PROMPTBOX],
        env=env,
        cwd=REPO,
        stdout=log_fh,
        stderr=subprocess.STDOUT,
    )

    # Wait for demo window to auto-open
    time.sleep(3.5)

    hwnd = None
    for _ in range(20):
        hwnd, rect = find_promptbox_window()
        if hwnd:
            break
        time.sleep(0.3)

    if not hwnd:
        print("[demo] ERROR: PromptBox window not found")
        proc.kill()
        log_fh.close()
        print(f"[demo] --- child log ({log_path}) ---")
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            print(f.read())
        sys.exit(1)

    print(f"[demo] window found at {rect}, capturing...")
    time.sleep(0.6)
    img = capture_window_clean(hwnd)

    out = os.path.join(OUT_DIR, "main.png")
    img.save(out)
    print(f"[demo] saved {out}  size={img.size}")

    # Shutdown
    print("[demo] cleaning up...")
    try:
        win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
    except Exception:
        pass
    time.sleep(0.5)
    proc.terminate()
    time.sleep(0.5)
    if proc.poll() is None:
        proc.kill()

    print("[demo] done.")


if __name__ == "__main__":
    main()
