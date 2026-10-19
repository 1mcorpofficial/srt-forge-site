#!/usr/bin/env python3
"""Surenka savarankišką SRT Forge Lite paketą dabartinei OS (be reikalavimo vartotojui turėti Python).

  pip install pyinstaller faster-whisper imageio-ffmpeg cryptography
  python lite/packaging/build.py [--out dist-lite]

Išvestis (--out kataloge):
  Windows: SRT-Forge-Lite-<v>-windows-x64.exe   (Inno Setup; jei yra iscc) arba .zip
  macOS:   SRT-Forge-Lite-<v>-macos-<arm64|x64>.dmg
  Linux:   SRT-Forge-Lite-<v>-linux-x64.tar.gz  (su Install failu)
"""
import argparse
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
LITE = os.path.dirname(HERE)
sys.path.insert(0, LITE)
from srtforge_lite import __version__  # noqa: E402

NAME = "SRT Forge Lite"


def run(cmd, **kw):
    print("+", " ".join(map(str, cmd)), flush=True)
    subprocess.run(cmd, check=True, **kw)


def pyinstaller(workdir: str, windowed: bool) -> str:
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--name", NAME,
           "--distpath", os.path.join(workdir, "dist"), "--workpath", os.path.join(workdir, "work"),
           "--specpath", workdir, "--paths", LITE,
           "--collect-all", "faster_whisper", "--collect-all", "ctranslate2", "--collect-all", "av",
           "--collect-all", "imageio_ffmpeg", "--collect-all", "tokenizers", "--collect-all", "onnxruntime",
           "--collect-all", "certifi", "--collect-data", "srtforge_lite",
           "--hidden-import", "srtforge_lite.server", "--hidden-import", "srtforge_lite.updater",
           "--add-data", os.path.join(LITE, "srtforge_lite", "ui.html") + os.pathsep + "srtforge_lite"]
    if windowed:
        cmd.append("--windowed")
    cmd.append(os.path.join(HERE, "entry.py"))
    run(cmd)
    return os.path.join(workdir, "dist")


def write_public_key(dist_dir: str, public_key: str) -> None:
    for root, _dirs, files in os.walk(dist_dir):
        if os.path.basename(root) == "srtforge_lite" and "ui.html" in files:
            with open(os.path.join(root, "update_public_key.txt"), "w", encoding="ascii") as handle:
                handle.write(public_key.strip() + "\n")
            return


def build_linux(dist: str, out: str) -> str:
    folder = os.path.join(dist, NAME)
    install = os.path.join(folder, "Install.sh")
    with open(install, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(f"""#!/bin/sh
# {NAME}: įdiegia į ~/.local/share/srtforge-lite ir prideda programų meniu įrašą. Sudo nereikia.
set -eu
HERE="$(cd "$(dirname "$0")" && pwd)"
DEST="${{XDG_DATA_HOME:-$HOME/.local/share}}/srtforge-lite"
APPS="${{XDG_DATA_HOME:-$HOME/.local/share}}/applications"
mkdir -p "$DEST" "$APPS" "$HOME/.local/bin"
rm -rf "$DEST"/*
cp -R "$HERE"/. "$DEST"/
rm -f "$DEST/Install.sh"
ln -sf "$DEST/{NAME}" "$HOME/.local/bin/srtforge"
cat > "$APPS/srtforge-lite.desktop" <<DESK
[Desktop Entry]
Type=Application
Name={NAME}
Comment=Garso transkripcija į subtitrus
Exec=$DEST/{NAME}
Terminal=false
Categories=AudioVideo;
DESK
echo "Įdiegta. Ieškok programų meniu: {NAME}"
""")
    os.chmod(install, 0o755)
    os.chmod(os.path.join(folder, NAME), 0o755)
    target = os.path.join(out, f"SRT-Forge-Lite-{__version__}-linux-x64.tar.gz")
    with tarfile.open(target, "w:gz") as tar:
        tar.add(folder, arcname=f"SRT-Forge-Lite-{__version__}")
    return target


def build_macos(dist: str, out: str) -> str:
    app = os.path.join(dist, f"{NAME}.app")
    run(["codesign", "--force", "--deep", "--sign", "-", app])          # ad-hoc: Apple Silicon reikalauja bent tokio
    arch = "arm64" if platform.machine() == "arm64" else "x64"
    target = os.path.join(out, f"SRT-Forge-Lite-{__version__}-macos-{arch}.dmg")
    stage = tempfile.mkdtemp(prefix="srtforge-dmg-")
    shutil.copytree(app, os.path.join(stage, f"{NAME}.app"), symlinks=True)
    os.symlink("/Applications", os.path.join(stage, "Applications"))
    if os.path.exists(target):
        os.remove(target)
    run(["hdiutil", "create", "-volname", NAME, "-srcfolder", stage, "-ov", "-format", "UDZO", target])
    shutil.rmtree(stage, ignore_errors=True)
    return target


def build_windows(dist: str, out: str) -> str:
    folder = os.path.join(dist, NAME)
    iscc = shutil.which("iscc") or r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
    if os.path.exists(iscc):
        script = os.path.join(HERE, "windows.iss")
        run([iscc, f"/DAppVersion={__version__}", f"/DSourceDir={folder}", f"/DOutputDir={out}", script])
        return os.path.join(out, f"SRT-Forge-Lite-{__version__}-windows-x64.exe")
    target = os.path.join(out, f"SRT-Forge-Lite-{__version__}-windows-x64.zip")
    shutil.make_archive(target[:-4], "zip", dist, NAME)
    return target


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=os.path.join(os.path.dirname(LITE), "dist-lite"))
    parser.add_argument("--public-key", default=os.environ.get("SRTFORGE_UPDATE_PUBLIC_KEY", ""))
    parser.add_argument("--console", action="store_true", help="derinimui: be --windowed (konsolinė programa)")
    args = parser.parse_args()
    out = os.path.abspath(args.out)
    os.makedirs(out, exist_ok=True)

    system = platform.system()
    with tempfile.TemporaryDirectory(prefix="srtforge-pyi-") as work:
        dist = pyinstaller(work, windowed=system in ("Windows", "Darwin") and not args.console)
        if args.public_key:
            write_public_key(dist, args.public_key)
        if system == "Linux":
            result = build_linux(dist, out)
        elif system == "Darwin":
            result = build_macos(dist, out)
        elif system == "Windows":
            result = build_windows(dist, out)
        else:
            sys.exit(f"Nepalaikoma OS: {system}")
    print("OK", result, f"{os.path.getsize(result) / 1e6:.0f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
