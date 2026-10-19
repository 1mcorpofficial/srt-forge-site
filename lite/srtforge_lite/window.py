"""Programos langas be naršyklės juostos: macOS – savas WKWebView langas (pywebview), kitur – Chrome/Edge „app“ režimas."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from typing import List, Optional

TITLE = "SRT Forge"
SIZE = (1120, 860)

_CHROMIUM = {
    "win32": [r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe", r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
              r"%ProgramFiles%\Google\Chrome\Application\chrome.exe", r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
              r"%LocalAppData%\Google\Chrome\Application\chrome.exe", r"%ProgramFiles%\BraveSoftware\Brave-Browser\Application\brave.exe"],
    "darwin": ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
               "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser", "/Applications/Chromium.app/Contents/MacOS/Chromium"],
    "linux": ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "microsoft-edge", "brave-browser"],
}


def find_chromium() -> Optional[str]:
    key = "win32" if sys.platform.startswith("win") else "darwin" if sys.platform == "darwin" else "linux"
    for candidate in _CHROMIUM[key]:
        path = os.path.expandvars(candidate)
        if key == "linux":
            path = shutil.which(candidate) or ""
        if path and os.path.isfile(path):
            return path
    return None


def chromium_command(binary: str, url: str, profile: str) -> List[str]:
    # atskiras profilis: procesas nesusilieja su vartotojo naršykle ir jo laukimas tikrai baigiasi uždarius langą
    return [binary, f"--app={url}", f"--user-data-dir={profile}", f"--window-size={SIZE[0]},{SIZE[1]}",
            "--no-first-run", "--no-default-browser-check", "--disable-features=Translate"]


def have_webview() -> bool:
    if sys.platform != "darwin":
        return False
    try:
        import webview  # noqa: F401

        return True
    except Exception:  # noqa: BLE001
        return False


def describe() -> str:
    if have_webview():
        return "window:webview"
    binary = find_chromium()
    return f"window:chromium:{binary}" if binary else "window:none"


def open_window(url: str, profile_dir: str) -> bool:
    """Atidaro langą ir laukia, kol jis uždarytas. False = tinkamo lango nėra (kviečiantysis atidaro naršyklę)."""
    if have_webview():
        try:
            import webview

            webview.create_window(TITLE, url, width=SIZE[0], height=SIZE[1], min_size=(720, 520))
            webview.start()
            return True
        except Exception:  # noqa: BLE001
            pass
    binary = find_chromium()
    if not binary:
        return False
    try:
        os.makedirs(profile_dir, exist_ok=True)
        started = subprocess.Popen(chromium_command(binary, url, profile_dir), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        code = started.wait()
        return code == 0
    except OSError:
        return False
