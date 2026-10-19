#!/bin/sh
# SRT Forge Lite diegimas (macOS ir Linux). Naudojimas:
#   curl -fsSL https://srt.1millionads.net/install.sh | sh
#   sh install.sh --uninstall
# Be sudo. Diegia į ~/.srtforge-lite (venv) ir ~/.local/bin/srtforge.
set -eu

BASE_URL="${SRTFORGE_BASE_URL:-https://srt.1millionads.net}"
PUBKEY="${SRTFORGE_PUBKEY:-N44iKMMaN7P1GuH4iI/sYa4OhkVdvjAGlzhy193WMSA=}"
HOME_DIR="${SRTFORGE_HOME:-$HOME/.srtforge-lite}"
BIN_DIR="${SRTFORGE_BIN:-$HOME/.local/bin}"

say() { printf '%s\n' "$*"; }
die() { printf 'KLAIDA: %s\n' "$*" >&2; exit 1; }

if [ "${1:-}" = "--uninstall" ]; then
    rm -rf "$HOME_DIR" "$BIN_DIR/srtforge" "$HOME/Applications/SRT Forge Lite.command" \
        "${XDG_DATA_HOME:-$HOME/.local/share}/applications/srtforge-lite.desktop"
    say "SRT Forge Lite pašalinta. Atsisiųsti modeliai (jei buvo) yra ~/.cache/huggingface – jų neliečiau."
    exit 0
fi

[ "$(id -u)" -ne 0 ] || [ "${SRTFORGE_ALLOW_ROOT:-}" = "1" ] || die "Nepaleisk su sudo/root – diegiama tik į tavo namų katalogą."
case "$BASE_URL$PUBKEY" in *__*) die "Tai šablono failas. Parsisiųsk install.sh iš svetainės." ;; esac
command -v curl >/dev/null 2>&1 || die "Reikia curl."

# 1. Python 3.9–3.13 (ctranslate2 ratai)
PY=""
for candidate in python3.12 python3.13 python3.11 python3.10 python3.9 python3; do
    if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; sys.exit(0 if (3,9) <= sys.version_info[:2] <= (3,13) else 1)' 2>/dev/null; then
        PY="$candidate"; break
    fi
done
if [ -z "$PY" ]; then
    case "$(uname -s)" in
        Darwin) die "Nerastas Python 3.9–3.13. Įdiek: brew install python@3.12  (arba https://www.python.org/downloads/macos/) ir paleisk iš naujo." ;;
        *)      die "Nerastas Python 3.9–3.13 su venv. Debian/Ubuntu: sudo apt install python3 python3-venv ; Fedora: sudo dnf install python3 ; tada paleisk iš naujo." ;;
    esac
fi
"$PY" -c 'import venv, ensurepip' 2>/dev/null || die "Trūksta python venv modulio (Debian/Ubuntu: sudo apt install python3-venv)."

# 2. Aplinka ir priklausomybės (statinis ffmpeg ateina per imageio-ffmpeg – sisteminio nereikia)
say "Kuriama aplinka: $HOME_DIR"
mkdir -p "$HOME_DIR" "$BIN_DIR"
"$PY" -m venv "$HOME_DIR/venv"
VPY="$HOME_DIR/venv/bin/python"
"$VPY" -m pip install --quiet --upgrade pip
say "Diegiamos priklausomybės (kelios minutės, ~300 MB)..."
"$VPY" -m pip install --quiet "faster-whisper>=1.0" "av>=11,<19" "imageio-ffmpeg>=0.4" "cryptography>=41" "sherpa-onnx>=1.10" "sentencepiece>=0.1.99"

# 3. Manifestas -> parašas -> sha256 -> diegimas
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
curl -fsSL "$BASE_URL/lite/latest.json" -o "$TMP/latest.json" || die "Nepavyko gauti $BASE_URL/lite/latest.json"
"$VPY" - "$TMP/latest.json" "$PUBKEY" "$TMP" <<'PYEOF' || die "Atnaujinimo paketo patikra nepavyko – diegimas nutrauktas."
import base64, hashlib, json, sys, urllib.request
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
manifest = json.load(open(sys.argv[1]))
url = manifest["downloadURL"]
if not url.startswith(("https://", "file://")):
    sys.exit("adresas ne https")
msg = "srtforge-update-v2|%s|%s|%s|%s" % (manifest["version"], manifest.get("build", ""), manifest["sha256"].lower(), url)
Ed25519PublicKey.from_public_bytes(base64.b64decode(sys.argv[2])).verify(base64.b64decode(manifest["signature"]), msg.encode())
data = urllib.request.urlopen(url, timeout=60).read()
if hashlib.sha256(data).hexdigest() != manifest["sha256"].lower():
    sys.exit("sha256 nesutampa")
open(sys.argv[3] + "/pkg.tar.gz", "wb").write(data)
print("Parašas ir sha256 patikrinti: SRT Forge Lite", manifest["version"])
PYEOF
"$VPY" -m pip install --quiet --no-deps --upgrade "$TMP/pkg.tar.gz"
printf '%s\n' "$PUBKEY" > "$("$VPY" -c 'import srtforge_lite,os;print(os.path.dirname(srtforge_lite.__file__))')/update_public_key.txt"

# 4. Paleidimo komanda ir ikonos vieta
cat > "$BIN_DIR/srtforge" <<LAUNCH
#!/bin/sh
exec "$VPY" -m srtforge_lite "\$@"
LAUNCH
chmod +x "$BIN_DIR/srtforge"

case "$(uname -s)" in
    Darwin)
        mkdir -p "$HOME/Applications"
        printf '#!/bin/sh\nexec "%s" -m srtforge_lite\n' "$VPY" > "$HOME/Applications/SRT Forge Lite.command"
        chmod +x "$HOME/Applications/SRT Forge Lite.command"
        say "Paleidimas: dukart spustelėk ~/Applications/SRT Forge Lite.command (arba terminale: srtforge)" ;;
    *)
        APPS="${XDG_DATA_HOME:-$HOME/.local/share}/applications"; mkdir -p "$APPS"
        printf '[Desktop Entry]\nType=Application\nName=SRT Forge Lite\nComment=Garso transkripcija į subtitrus\nExec=%s -m srtforge_lite\nTerminal=false\nCategories=AudioVideo;\n' "$VPY" > "$APPS/srtforge-lite.desktop"
        say "Paleidimas: programų meniu „SRT Forge Lite“ (arba terminale: srtforge)" ;;
esac

case ":$PATH:" in *":$BIN_DIR:"*) ;; *) say "Pastaba: pridėk $BIN_DIR į PATH, kad veiktų komanda „srtforge“." ;; esac
say "Paruošta. Pirmą kartą paleidus bus atsisiųstas kalbos modelis."
