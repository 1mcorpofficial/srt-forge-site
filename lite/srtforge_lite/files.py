"""Failų darbas montuotojui: natūralus failo pasirinkimo langas, rezultatai šalia originalo, subtitrų stiliai."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from typing import List, Optional

from .core import Rules

# stilius -> taisyklės: tipiški televizijos / socialinių tinklų / standartiniai nustatymai
STYLES = {
    "standard": Rules(),
    "tv": Rules(max_line=37, max_lines=2, min_duration=1.2, max_duration=6.0, min_gap=0.12, split_on_pause=0.8),
    "social": Rules(max_line=24, max_lines=2, min_duration=0.6, max_duration=3.5, min_gap=0.08, split_on_pause=0.5),
}
OUTPUTS = ("srt", "vtt", "txt", "json", "soft", "burn")
MEDIA_EXT = (".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".mxf", ".wav", ".mp3", ".m4a", ".flac", ".ogg", ".aac", ".opus", ".wma")


def unique_path(path: str) -> str:
    """Niekada neperrašo esamo failo: prideda ` (2)`, ` (3)`…"""
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(path)
    n = 2
    while os.path.exists(f"{stem} ({n}){ext}"):
        n += 1
    return f"{stem} ({n}){ext}"


def default_out_dir() -> str:
    return os.path.join(os.path.expanduser("~"), "SRT Forge")


def pick_files() -> Optional[List[str]]:
    """Sistemos failo pasirinkimo langas. None = šioje sistemoje neįmanoma (UI naudoja naršyklės pasirinkimą)."""
    try:
        if sys.platform == "darwin":
            script = ('set f to choose file with prompt "Pasirink garso arba video failus" with multiple selections allowed\n'
                      'set o to ""\nrepeat with i in f\nset o to o & POSIX path of i & linefeed\nend repeat\nreturn o')
            out = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=600)
        elif sys.platform.startswith("win"):
            ps = ("Add-Type -AssemblyName System.Windows.Forms;"
                  "$d=New-Object System.Windows.Forms.OpenFileDialog;$d.Multiselect=$true;"
                  "$d.Title='Pasirink garso arba video failus';"
                  "if($d.ShowDialog() -eq 'OK'){[Console]::OutputEncoding=[Text.Encoding]::UTF8;$d.FileNames -join \"`n\"}")
            out = subprocess.run(["powershell", "-NoProfile", "-STA", "-Command", ps], capture_output=True, text=True,
                                 encoding="utf-8", timeout=600)
        elif shutil.which("zenity"):
            out = subprocess.run(["zenity", "--file-selection", "--multiple", "--separator=\n",
                                  "--title=Pasirink garso arba video failus"], capture_output=True, text=True, timeout=600)
        elif shutil.which("kdialog"):
            out = subprocess.run(["kdialog", "--getopenfilename", "--multiple", "--separate-output", os.path.expanduser("~")],
                                 capture_output=True, text=True, timeout=600)
        else:
            return None
    except (OSError, subprocess.SubprocessError):
        return None
    return [line.strip() for line in out.stdout.splitlines() if line.strip() and os.path.isfile(line.strip())]


def reveal(path: str) -> bool:
    """Parodo failą failų tvarkyklėje."""
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", "-R", path])
        elif sys.platform.startswith("win"):
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
        else:
            subprocess.Popen(["xdg-open", os.path.dirname(path)])
        return True
    except OSError:
        return False
