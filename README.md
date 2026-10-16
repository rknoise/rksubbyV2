<p align="center"><img src="Logo.jpg" alt="rksubbyV2 logó" width="240"></p>

# rksubbyV2 – élő magyar felirat angol beszédhez

> **English summary:** rksubbyV2 turns English speech from a console (via a capture card such as the AVerMedia Live Gamer 4K) into live Hungarian subtitles. It shows the capture card's picture in its own always-on-top player window with the subtitles drawn on top, so OBS is no longer needed (an OBS browser source is still available). Speech is detected with Silero VAD, transcribed locally with faster-whisper on the GPU, and translated with DeepL. A one-click installer sets up Python, all packages, ffmpeg, the CUDA libraries and the speech model inside the project folder, and a setup wizard walks you through the rest. Typical delay from the end of a sentence to the Hungarian subtitle: about 0.6–0.8 s on an RTX 3090.

Az Xboxon (vagy bármilyen HDMI-forráson) futó angol beszédet élőben magyar felirattá alakítja. A capture kártya képét a program a **saját, mindig legfelül lévő lejátszóablakában** mutatja, rajta a felirattal. **OBS nem kell hozzá**, de ha használod, az OBS-be is beteheted a feliratot.

## Újdonságok a 2.1-ben

- **Telepítés egy kattintással:** az `install.bat` mindent letölt és beállít (Python, csomagok, ffmpeg, NVIDIA-gyorsítás, felismerő modell), és asztali ikont készít. Előre telepített Python nem kell.
- **Beállító varázsló:** grafikus ablak, ami lépésenként végigvezet a telepítéstől az indításig.
- **Saját lejátszó, OBS nélkül:** a kártya képe egy mindig legfelül lévő ablakban, rajta a felirat. A hangot a program a kártyáról olvassa, és a hangszóródon is lejátssza. VB-CABLE nem kell.
- **Átlátszó felirat-réteg:** csak a felirat, a képernyő alján, minden ablak fölött (pl. ha más programban nézed a képet).
- **Vezérlőpult:** indítás és leállítás, hangszint, késés, legutóbbi feliratok, DeepL-keret, szünet, SRT-mentés.
- **Játékprofilok:** nevek és helyek a jobb felismeréshez, glosszárium a fordításhoz (GTA V, GTA VI, RDR2, általános, sajátok).
- Gyorsabb és stabilabb működés: nincs fölösleges GPU-munka, a kártya vagy a bemenet elhalásakor magától újranyit, az OBS-felirat késés nélkül, élőben frissül.

## Telepítés

1. Töltsd le a projektet (zöld **Code** gomb → **Download ZIP**), és csomagold ki egy olyan mappába, amelynek az útvonalában **nincs ékezet**, pl. `C:\rksubbyV2`.
2. Futtasd az **`install.bat`**-ot. Kb. 5–15 perc, a sebesség az internettől függ. NVIDIA-kártyánál kb. 4 GB-ot tölt le (csomagok, CUDA-könyvtárak, modell), anélkül kevesebbet.
3. Indítsd az asztali **rksubby** ikonnal. Az első indításkor a beállító varázsló nyílik meg.

Minden a projektmappába kerül (`.venv`, `.tools`), a felismerő modell a Hugging Face gyorsítótárába. **Eltávolítás:** töröld a mappát és az asztali ikont.

### A varázsló lépései

1. **Telepítés:** ellenőrzi, megvan-e minden (Python, csomagok, NVIDIA, modell, capture kártya, ffmpeg, Edge). Ami hiányzik, egy gombbal pótolható.
2. **Forrás:** kiválasztod a képforrást (pl. `AVerMedia HD Capture GC573 1`) élő előnézettel. A hangforrás magától a kártya hangjára áll, hangszintjelzővel. Itt kapcsolhatod be, hogy a hang a gépeden is szóljon.
3. **Fordítás:** DeepL-kulcs (beillesztés, ellenőrzés, havi keret), tegező vagy magázó hangnem, próbafordítás. Választható helyi AI (Ollama) vagy Google is.
4. **Játékprofil:** nevek és helyek, glosszárium (angol → magyar).
5. **Megjelenítés:** lejátszó (mindig legfelül), átlátszó felirat-réteg, OBS-cím; betűméret, szín, háttérdoboz, szélesség, élő előnézettel.
6. **Próba és indítás:** próba egy angol mintafelvétellel, aztán a lejátszó és a feliratozás indítása.

A beállítás után az asztali ikon **rögtön a lejátszót nyitja meg, és a feliratozás magától indul**. A beállításokhoz a lejátszó fogaskerék-gombjával jutsz vissza.

## A lejátszó

- A kép a kártyáról közvetlenül jön (1080p60 vagy a választott felbontás), a felirat a kép alján jelenik meg.
- A vezérlősáv egérmozdításra jelenik meg: indítás/leállítás, hang és hangerő, felirat be/ki, **mindig legfelül**, teljes képernyő, beállítások.
- Billentyűk: **F** vagy dupla kattintás = teljes képernyő, **M** = hang, **S** = felirat, **T** = mindig legfelül, **Szóköz** = feliratozás indítása vagy leállítása.

## Hogyan működik

```
Xbox -> capture kártya --kép--> lejátszóablak (Edge alkalmazásablak, mindig legfelül) <-- felirat
                       --hang--> ffmpeg -> Silero VAD -> faster-whisper (GPU) -> DeepL -> felirat
                                       \-> hangszóró
```

A program egy helyi webszervert futtat (`http://127.0.0.1:8765/`). A beállító ablak, a lejátszó és az OBS-felirat is innen kapja az adatokat. A szerver csak a saját gépről érhető el, és más weboldalakról érkező kéréseket nem fogad el.

Az **AVerMedia Live Gamer 4K (GC573)** hangja nem külön Windows-hangeszköz, hanem a kártya DirectShow-eszközén jön a képpel együtt. Ezt az ffmpeg olvassa ki, a telepítő ezt is felteszi. A kártyát a lejátszó és a feliratozó egyszerre használja. USB-s kártyáknál a hang gyakran külön Windows-hangbemenet, azt is választhatod.

## Mért teljesítmény

RTX 3090, `distil-large-v3`, DeepL API Free, élő angol YouTube-videó az Xboxon (2026. október):

| | |
|---|---|
| Mondat vége → magyar felirat (medián, 143 mondat) | **~0,8 mp** |
| Szünettel záruló mondatoknál | 0,55–0,7 mp |
| Gyors, szünet nélküli beszédnél | 1,5–2,5 mp |
| Beszédfelismerés mondatonként | ~110–230 ms |
| DeepL-fordítás mondatonként | ~300 ms |
| Indulás (meleg / hidegindítás) | ~4–9 mp / ~40 mp |

## Követelmények

- Windows 10 vagy 11, Microsoft Edge (a Windows része)
- NVIDIA videokártya, ajánlott (6+ GB VRAM). Nélküle CPU-n is elindul, de lassabb és pontatlanabb.
- Capture kártya (pl. AVerMedia Live Gamer 4K), vagy OBS + [VB-Audio Virtual Cable](https://vb-audio.com/Cable/)
- DeepL API Free kulcs (ingyenes, havi 500 000 karakter; a regisztrációnál a DeepL bankkártyás azonosítást kér)

## DeepL-kulcs

1. Regisztrálj a DeepL API Free csomagra: <https://www.deepl.com/pro-api>.
2. Másold ki a kulcsot: <https://www.deepl.com/your-account/keys>. A Free kulcs vége `:fx`.
3. Illeszd be a varázsló **Fordítás** lépésénél. A program ellenőrzi, és kiírja a havi keretet.

A kulcs a felhasználói mappádba kerül (`%USERPROFILE%\.rksubby\deepl_key.txt`), **nem** a projektmappába, így nem adod tovább véletlenül. A vezérlőpulton látod, mennyi karaktert küldött el, és mennyi ment el előre fordításra. Ha fogy a keret, a Fordítás lépésnél kikapcsolhatod az előre fordítást.

## OBS-sel (nem kötelező)

Ha az OBS-ben nézed vagy rögzíted a játékot:

1. **Hang:** OBS Beállítások → Hang → Speciális → **Hangfigyelő eszköz: CABLE Input (VB-Audio Virtual Cable)**. A capture-forrásnál a Hangfigyelés legyen „Figyelés és kimenet”, az Asztali hangnál „Figyelés kikapcsolása”. A varázslóban a hangforrás: `CABLE Output`.
2. **Felirat:** új **Böngészőforrás**, a „Helyi fájl” kikapcsolva, a **Webcím `http://127.0.0.1:8765/`**, a méret 1920×1080, a forrás a lista tetején.

A feliratokat SRT-fájlba is mentheted a felvételhez (Vezérlőpult → Feliratok mentése SRT-be). A fájlok a `feliratok` mappába kerülnek.

## Parancssor (haladóknak)

| Parancs | Mit csinál |
|---|---|
| `start.bat` | ugyanaz, mint az ikon, de látszik a konzol (hibakereséshez) |
| `start.bat --setup` | a beállító ablakkal indul |
| `start.bat --cli` | ablak nélkül, konzolban (a régi működés) |
| `start.bat --cli --card "AVerMedia HD Capture GC573 1"` | konzolban, a kártya hangjával |
| `start.bat --wav teszt_angol.wav` | próba a mintafelvétellel, Xbox nélkül |
| `start.bat --list-devices` | hangbemenetek és capture kártyák listája |
| `start.bat --test-translate` | a fordítók gyors tesztje |
| `start.bat --deepl-key` | DeepL-kulcs beállítása a konzolon |

A beállítások helye: `%USERPROFILE%\.rksubby\config.json`. A varázslóban és a **Haladó beállítások** oldalon állíthatók, egy új verzió letöltésekor nem vesznek el. Ikonnal indítva a program kimenete a `%USERPROFILE%\.rksubby\rksubby.log` fájlba kerül. Ha minden ablakát bezárod, másfél perc múlva magától kilép.

## Hibaelhárítás

**Nincs kép a lejátszóban.** Be van kapcsolva a konzol? Ha a kártya saját programja (AVerMedia RECentral) vagy az OBS kizárólagosan használja a kártyát, zárd be. A varázsló **Forrás** lépésénél nézd meg az előnézetet.

**„NEM JÖN HANG”.** Capture kártyánál: szól a játék vagy videó? A varázsló **Forrás** lépésén a hangszintjelzőnek mozognia kell. OBS + VB-CABLE esetén a Windows-frissítés után az OBS megtarthat egy már nem létező eszközt: a Hangfigyelő eszköznél válassz ki egy másikat, Alkalmaz, aztán vissza a CABLE Inputot, és megint Alkalmaz.

**„A GPU-s betöltés nem sikerült”.** Futtasd újra az `install.bat`-ot, vagy a varázsló **Telepítés** lépésénél nyomd meg az „NVIDIA-gyorsítás telepítése” gombot.

**DeepL-hibák.** 403: rossz a kulcs, add meg újra a Fordítás lépésnél. 456: elfogyott a havi keret, ilyenkor a hónap végéig angol felirat jelenik meg (vagy a helyi AI fordít, ha fut az Ollama).

**A 8765-ös port foglalt.** Fut még egy példány? Ha igen, az ikon az ő ablakát nyitja meg. Különben állíts be másik portot (Haladó beállítások), és írd át az OBS-ben is a címet.

## Felépítés

```
rksubby.py            indító
subby/                a program
  cli.py              indítás, parancssor
  engine.py           a feliratozó motorja (modell, fordító, hangforrás indítása és leállítása)
  subtitler.py        mondatdarabolás, felismerés, korai lezárás
  capture.py          capture kártya hangja (ffmpeg), hangszóró, hangszintmérő
  audio.py            Windows-hangbemenetek, WAV
  translate.py        DeepL (glosszáriummal), Ollama, Google
  display.py          feliratállapot, napló, SRT
  server.py, api.py   helyi webszerver és a felület hívásai
  windows.py          ablakok (Edge-alkalmazásablak, mindig legfelül)
  overlay_qt.py       átlátszó felirat-réteg (külön folyamat)
  web/                beállító varázsló, vezérlőpult, lejátszó, OBS-felirat
profiles/             beépített játékprofilok
tools/install.ps1     a telepítő
tests/                tesztek: python -m unittest discover -s tests
```

## Adatvédelem

A beszédfelismerés helyben fut, csak a felismert angol szöveg megy a fordítóhoz (DeepL vagy Google). Helyi AI (Ollama) esetén semmi nem hagyja el a gépet. A `sub_log.tsv` helyben tárolja a felismert mondatokat; ha nem kell, kikapcsolhatod (Haladó beállítások), továbbadás előtt pedig hagyd ki.

## Felhasznált projektek

[faster-whisper](https://github.com/SYSTRAN/faster-whisper) (MIT), [Whisper](https://github.com/openai/whisper) és [distil-whisper](https://github.com/huggingface/distil-whisper) modellek, [Silero VAD](https://github.com/snakers4/silero-vad) (MIT), [DeepL API](https://www.deepl.com/pro-api), [FFmpeg](https://ffmpeg.org) ([imageio-ffmpeg](https://github.com/imageio/imageio-ffmpeg)), [PyQt5](https://riverbankcomputing.com/software/pyqt/) (GPL), [uv](https://github.com/astral-sh/uv), [OBS Studio](https://obsproject.com), [VB-Audio Virtual Cable](https://vb-audio.com/Cable/).
A `teszt_angol.wav` J. F. Kennedy 1961-es beiktatási beszédéből származik (közkincs).

Fejlesztés: RKnoise, Claude (Anthropic) segítségével.

## Licenc

MIT, lásd a `LICENSE` fájlt.
