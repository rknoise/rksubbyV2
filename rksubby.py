# -*- coding: utf-8 -*-
"""
rksubbyV2 - élő magyar feliratozó angol beszédhez (Xbox / bármilyen HDMI-forrás + OBS)

Útvonal: capture kártya -> OBS (hangfigyelés) -> VB-CABLE -> faster-whisper (GPU) -> DeepL -> OBS-felirat

Használat
  python rksubby.py                         beállító varázsló és vezérlőpult (külön ablakban)
  python rksubby.py --start                 ugyanez, és rögtön élőben indul
  python rksubby.py --cli                   konzolos mód, ablak nélkül (a régi működés)
  python rksubby.py --wav teszt_angol.wav   próba egy felvétellel, Xbox nélkül (konzolos mód)
  python rksubby.py --deepl-key             DeepL API-kulcs beállítása a konzolon
  python rksubby.py --test-translate        a fordítók gyors tesztje (Whisper nélkül)
  python rksubby.py --list-devices          bemeneti eszközök listája
  python rksubby.py --overlay               OBS nélkül: beépített, átlátszó felirat-ablak (--screen N)
  python rksubby.py --debug                 beszédszakaszok kiírása a konzolra

OBS böngészőforrás: http://127.0.0.1:8765/
A beállítások helye: %USERPROFILE%\\.rksubby\\config.json (a varázslóban állíthatók).
A program felépítése: subby\\ mappa (README.md, "Felépítés").
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from subby.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
