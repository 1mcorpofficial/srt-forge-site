"""Įėjimo taškas supakuotai programai (PyInstaller): dukart spustelėjus atidaro naršyklę, uždarius langą išsijungia."""
import os
import sys


def _redirect_output_when_windowed() -> None:
    # Windows --windowed režime stdout/stderr yra None: rašome į žurnalą, kad klaidos nedingtų.
    if sys.stdout is None or sys.stderr is None:
        from srtforge_lite.server import data_dir

        log = open(os.path.join(data_dir(), "srtforge.log"), "a", buffering=1, encoding="utf-8")
        sys.stdout = sys.stdout or log
        sys.stderr = sys.stderr or log


def main() -> None:
    _redirect_output_when_windowed()
    if len(sys.argv) == 1:
        sys.argv += ["--idle-exit", "180"]
    from srtforge_lite.server import main as run

    run()


if __name__ == "__main__":
    main()
