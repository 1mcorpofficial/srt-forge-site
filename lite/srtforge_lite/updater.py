"""Atnaujinimas: latest.json (Ed25519 + sha256, tas pats formatas kaip SRT Forge Mac) -> pip install iš patikrinto tar.gz."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import platform
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from typing import Optional, Tuple

from . import __version__

DEFAULT_MANIFEST_URL = "https://srt.1millionads.net/lite/latest.json"
DOWNLOADS_URL = "https://srt.1millionads.net/downloads.json"
PUBKEY_FILE = os.path.join(os.path.dirname(__file__), "update_public_key.txt")


class UpdateError(Exception):
    pass


def public_key() -> str:
    try:
        with open(PUBKEY_FILE, encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError:
        return ""


def message(version: str, build: str, sha256: str, url: str) -> bytes:
    return f"srtforge-update-v2|{version}|{build or ''}|{sha256.lower()}|{url}".encode("utf-8")


def verify_manifest(manifest: dict, public_key_b64: str) -> None:
    """Ed25519 parašas: versija+build+sha256+URL. Be galiojančio parašo – klaida (fail-closed)."""
    sha, sig = manifest.get("sha256"), manifest.get("signature")
    if not sha or not sig:
        raise UpdateError("Manifestas neturi sha256 arba parašo.")
    if not public_key_b64:
        raise UpdateError("Programoje nėra atnaujinimų viešojo rakto.")
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64))
        key.verify(base64.b64decode(sig), message(manifest["version"], manifest.get("build", ""), sha, manifest["downloadURL"]))
    except InvalidSignature as error:
        raise UpdateError("Atnaujinimo parašas neteisingas.") from error
    except (ValueError, KeyError, TypeError) as error:
        raise UpdateError(f"Netinkamas manifestas ar raktas: {error}") from error


def parse_version(text: str) -> Tuple[int, ...]:
    return tuple(int(part) if part.isdigit() else 0 for part in text.split("."))


def is_newer(candidate: str, current: str) -> bool:
    a, b = parse_version(candidate), parse_version(current)
    width = max(len(a), len(b))
    return a + (0,) * (width - len(a)) > b + (0,) * (width - len(b))


def fetch(url: str, allow_local: bool = False, timeout: int = 30) -> bytes:
    if not (url.startswith("https://") or (allow_local and (url.startswith("http://127.0.0.1") or url.startswith("file://")))):
        raise UpdateError("Atsisiuntimo adresas turi būti https.")
    with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310 - schema patikrinta aukščiau
        return response.read()


def check(manifest_url: str = DEFAULT_MANIFEST_URL, key: Optional[str] = None, allow_local: bool = False) -> Optional[dict]:
    """Grąžina patikrintą manifestą, jei yra naujesnė versija; kitaip None."""
    manifest = json.loads(fetch(manifest_url, allow_local))
    verify_manifest(manifest, public_key() if key is None else key)
    return manifest if is_newer(manifest["version"], __version__) else None


def install(manifest: dict, key: Optional[str] = None, allow_local: bool = False, python: str = sys.executable) -> str:
    """Atsisiunčia, patikrina sha256 PRIEŠ diegdamas, tada `pip install`. Nepavykus sena versija lieka."""
    verify_manifest(manifest, public_key() if key is None else key)
    data = fetch(manifest["downloadURL"], allow_local)
    actual = hashlib.sha256(data).hexdigest()
    if actual != manifest["sha256"].lower():
        raise UpdateError(f"sha256 nesutampa (laukta {manifest['sha256'][:12]}…, gauta {actual[:12]}…). Diegimas nutrauktas.")
    with tempfile.TemporaryDirectory() as tmp:
        archive = os.path.join(tmp, "srtforge_lite-" + manifest["version"] + ".tar.gz")
        with open(archive, "wb") as handle:
            handle.write(data)
        result = subprocess.run([python, "-m", "pip", "install", "--quiet", "--no-deps", "--upgrade", archive],
                                capture_output=True, text=True)
        if result.returncode != 0:
            raise UpdateError("pip install nepavyko: " + (result.stderr.strip().splitlines() or ["?"])[-1])
    return manifest["version"]


def platform_key() -> Tuple[str, str]:
    system = {"Windows": "windows", "Darwin": "macos"}.get(platform.system(), "linux")
    machine = platform.machine().lower()
    return system, ("arm64" if machine in ("arm64", "aarch64") else "x64")


def check_installer(url: str = DOWNLOADS_URL, key: Optional[str] = None, allow_local: bool = False,
                    target: Optional[Tuple[str, str]] = None) -> Optional[dict]:
    """Supakuotos programos atnaujinimas: downloads.json -> šiai OS tinkamas failas, jei versija naujesnė ir parašas geras."""
    manifest = json.loads(fetch(url, allow_local))
    if not is_newer(manifest["version"], __version__):
        return None
    wanted = target or platform_key()
    for entry in manifest.get("files", []):
        if (entry.get("platform"), entry.get("arch")) != wanted:
            continue
        candidate = {"version": manifest["version"], "build": manifest.get("build", ""), "downloadURL": entry["url"],
                     "sha256": entry["sha256"], "signature": entry["signature"]}
        verify_manifest(candidate, public_key() if key is None else key)
        return dict(entry, version=manifest["version"], build=manifest.get("build", ""))
    return None


def download_installer(entry: dict, directory: str, key: Optional[str] = None, allow_local: bool = False) -> str:
    """Atsisiunčia ir patikrina (parašas + sha256) diegimo failą. Grąžina kelią; blogo failo nepalieka diske."""
    verify_manifest({"version": entry["version"], "build": entry.get("build", ""), "downloadURL": entry["url"],
                     "sha256": entry["sha256"], "signature": entry["signature"]}, public_key() if key is None else key)
    data = fetch(entry["url"], allow_local, timeout=600)
    actual = hashlib.sha256(data).hexdigest()
    if actual != entry["sha256"].lower():
        raise UpdateError(f"sha256 nesutampa (laukta {entry['sha256'][:12]}…, gauta {actual[:12]}…). Failas neišsaugotas.")
    os.makedirs(directory, exist_ok=True)
    path = _unique_path(directory, _signed_filename(entry["url"]))
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)       # niekada neperrašo esamo failo
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
    return path


ALLOWED_EXTENSIONS = (".exe", ".dmg", ".tar.gz", ".zip")


def _signed_filename(url: str) -> str:
    """Vardas imamas iš PASIRAŠYTO URL (ne iš nepasirašyto `name` lauko) ir tik su leidžiamu plėtiniu."""
    name = os.path.basename(urllib.parse.unquote(urllib.parse.urlparse(url).path))
    if not name or name in (".", "..") or "/" in name or "\\" in name or not name.lower().endswith(ALLOWED_EXTENSIONS):
        raise UpdateError(f"Netinkamas diegimo failo vardas: {name!r}")
    return name


def _unique_path(directory: str, name: str) -> str:
    stem, ext = (name[:-7], ".tar.gz") if name.lower().endswith(".tar.gz") else os.path.splitext(name)
    candidate, counter = os.path.join(directory, name), 1
    while os.path.exists(candidate):
        candidate = os.path.join(directory, f"{stem}-{counter}{ext}")
        counter += 1
    return candidate


def open_file(path: str) -> None:
    if sys.platform.startswith("win"):
        os.startfile(path)  # type: ignore[attr-defined]  # noqa: S606
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


def run_cli() -> int:
    try:
        manifest = check()
        if not manifest:
            print(f"Naudoji naujausią versiją ({__version__}).")
            return 0
        print(f"Randama {manifest['version']} (dabar {__version__}). Diegiama...")
        install(manifest)
        print("Atnaujinta. Paleisk programą iš naujo.")
        return 0
    except (UpdateError, OSError, ValueError) as error:
        print(f"Atnaujinti nepavyko: {error}", file=sys.stderr)
        return 1
