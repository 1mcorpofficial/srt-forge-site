# SRT Forge Lite diegimas (Windows 10/11). PowerShell:
#   irm https://srt.1millionads.net/install.ps1 | iex
# Be administratoriaus teisių. Diegia į %LOCALAPPDATA%\SRTForgeLite ir sukuria Start meniu nuorodą.
$ErrorActionPreference = 'Stop'
$BaseUrl = if ($env:SRTFORGE_BASE_URL) { $env:SRTFORGE_BASE_URL } else { 'https://srt.1millionads.net' }
$PubKey  = if ($env:SRTFORGE_PUBKEY)   { $env:SRTFORGE_PUBKEY }   else { 'N44iKMMaN7P1GuH4iI/sYa4OhkVdvjAGlzhy193WMSA=' }
$Home_   = if ($env:SRTFORGE_HOME)     { $env:SRTFORGE_HOME }     else { Join-Path $env:LOCALAPPDATA 'SRTForgeLite' }

function Fail($m) { Write-Host "KLAIDA: $m" -ForegroundColor Red; throw $m }
if (($BaseUrl + $PubKey) -match '__') { Fail 'Tai šablono failas. Parsisiųsk install.ps1 iš svetainės.' }

# 1. Python 3.9-3.13
$py = $null
foreach ($cand in @(@('py','-3.12'), @('py','-3.11'), @('py','-3.13'), @('py','-3.10'), @('python'))) {
    try {
        $exe = $cand[0]; $args_ = if ($cand.Count -gt 1) { @($cand[1]) } else { @() }
        $ok = & $exe @args_ -c "import sys; print(1 if (3,9) <= sys.version_info[:2] <= (3,13) else 0)" 2>$null
        if ($ok -eq '1') { $py = ,@($exe) + $args_; break }
    } catch {}
}
if (-not $py) {
    Write-Host 'Python 3.9-3.13 nerastas. Diegiu per winget...'
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements
        Fail 'Python įdiegtas. Uždaryk šį langą, atidaryk naują PowerShell ir paleisk diegimą iš naujo.'
    }
    Fail 'Įdiek Python 3.12 iš https://www.python.org/downloads/windows/ (pažymėk "Add to PATH") ir paleisk iš naujo.'
}

# 2. Aplinka
New-Item -ItemType Directory -Force -Path $Home_ | Out-Null
& $py[0] @($py | Select-Object -Skip 1) -m venv (Join-Path $Home_ 'venv')
$vpy = Join-Path $Home_ 'venv\Scripts\python.exe'
$vpyw = Join-Path $Home_ 'venv\Scripts\pythonw.exe'
& $vpy -m pip install --quiet --upgrade pip
Write-Host 'Diegiamos priklausomybės (kelios minutės, ~300 MB)...'
& $vpy -m pip install --quiet --only-binary=:all: 'faster-whisper>=1.0' 'av>=11,<19' 'imageio-ffmpeg>=0.4' 'cryptography>=41' 'sherpa-onnx>=1.10' 'sentencepiece>=0.1.99'
if ($LASTEXITCODE -ne 0) { throw 'Nepavyko įdiegti priklausomybių. Atsisiųsk paruoštą .exe iš https://srt.1millionads.net' }

# 3. Manifestas -> parašas -> sha256 -> diegimas
$tmp = Join-Path ([IO.Path]::GetTempPath()) ("srtforge-" + [guid]::NewGuid())
New-Item -ItemType Directory -Path $tmp | Out-Null
try {
    Invoke-WebRequest -UseBasicParsing "$BaseUrl/lite/latest.json" -OutFile (Join-Path $tmp 'latest.json')
    $verify = @'
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
'@
    $script = Join-Path $tmp 'verify.py'
    Set-Content -Path $script -Value $verify -Encoding UTF8
    & $vpy $script (Join-Path $tmp 'latest.json') $PubKey $tmp
    if ($LASTEXITCODE -ne 0) { Fail 'Atnaujinimo paketo patikra nepavyko – diegimas nutrauktas.' }
    & $vpy -m pip install --quiet --no-deps --upgrade (Join-Path $tmp 'pkg.tar.gz')
    $pkgDir = & $vpy -c "import srtforge_lite,os;print(os.path.dirname(srtforge_lite.__file__))"
    Set-Content -Path (Join-Path $pkgDir 'update_public_key.txt') -Value $PubKey -Encoding ASCII
} finally { Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue }

# 4. Paleidimas: Start meniu nuoroda + srtforge.cmd
$cmd = Join-Path $Home_ 'srtforge.cmd'
Set-Content -Path $cmd -Value "@echo off`r`n`"$vpy`" -m srtforge_lite %*" -Encoding ASCII
$shell = New-Object -ComObject WScript.Shell
$lnk = $shell.CreateShortcut((Join-Path ([Environment]::GetFolderPath('Programs')) 'SRT Forge Lite.lnk'))
$lnk.TargetPath = $vpyw
$lnk.Arguments = '-m srtforge_lite'
$lnk.WorkingDirectory = $Home_
$lnk.Description = 'Garso transkripcija į subtitrus'
$lnk.Save()
Write-Host 'Paruošta. Paleisk: Start meniu -> "SRT Forge Lite". Pirmą kartą bus atsisiųstas kalbos modelis.'
Write-Host "Pašalinti: ištrink $Home_ ir Start meniu nuorodą."
