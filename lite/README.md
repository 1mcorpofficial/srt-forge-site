# SRT Forge Lite

Vietinė garso/video transkripcija į SRT/VTT/TXT. Windows, macOS, Linux. Python + faster-whisper + ffmpeg (per imageio-ffmpeg).

- Vartotojams: diegimo komandos svetainėje (`site/index.html`).
- Paleidimas iš šaltinių: `pip install -e lite && srtforge`
- Atnaujinimas: `srtforge update` (Ed25519 + sha256, fail-closed).
- Testai: `python -m unittest discover -s lite/tests -t lite` (tikras ffmpeg/faster-whisper testai praleidžiami, jei nėra modelio).
- Leidimas: `Scripts/publish_site.sh` (žr. Docs/DISTRIBUTION.md).

Saugumas: serveris klauso tik 127.0.0.1, atsitiktinis prievadas, sesijos tokenas, Host patikra (DNS rebinding), įkelti failai trinami po darbo.
