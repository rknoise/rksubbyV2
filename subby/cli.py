# -*- coding: utf-8 -*-
"""Indítás: alapból a grafikus beállító/vezérlő ablak, --cli / --wav esetén a régi konzolos mód."""

import argparse
import json
import os
import sys
import threading
import time
import urllib.request

from . import __version__
from .config import HOME_DIR, Config, load_deepl_key, save_deepl_key
from .textutil import extract_deepl_key, fmt_int, mask_key, short_err
from .windows import open_window


def say(msg=""):
    print(msg, flush=True)


# -----------------------------------------------------------------------------
# Második példány, napló ablak nélküli indításnál
# -----------------------------------------------------------------------------

def _log_to_file():
    """Konzol nélküli indításnál (pythonw, asztali ikon) a kimenet egy naplófájlba megy. True: nincs konzol."""
    if sys.stdout is not None and sys.stderr is not None:
        return False
    try:
        os.makedirs(HOME_DIR, exist_ok=True)
        f = open(os.path.join(HOME_DIR, "rksubby.log"), "a", encoding="utf-8", buffering=1)
        f.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
        sys.stdout = sys.stderr = f
    except OSError:
        pass
    return True


def _quit_without_windows(api, quit_event, grace_s=90):
    """Konzol nélkül (asztali ikon) a program ne maradjon a háttérben, ha minden ablakát bezárták:
    ha egy ideig egyik ablak sem jelentkezik, kilép (a kártyát és a GPU-t is elengedi)."""
    while not quit_event.wait(5.0):
        if api.last_ui and time.monotonic() - api.last_ui > grace_s:
            say(f"{grace_s} mp óta nincs nyitott ablak: kilépés.")
            quit_event.set()


def other_instance(port):
    """Fut-e már a program ezen a porton."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=0.6) as r:
            return json.loads(r.read().decode("utf-8")).get("app") == "rksubby"
    except Exception:
        return False


# -----------------------------------------------------------------------------
# Konzolos DeepL-kulcs beállítás és fordítóteszt
# -----------------------------------------------------------------------------

def read_clipboard_text():
    """A vágólap szövege (Windows: közvetlenül a rendszerből), hiba esetén üres."""
    try:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.WinDLL("user32", use_last_error=True)
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            user32.OpenClipboard.argtypes = [wintypes.HWND]
            user32.OpenClipboard.restype = wintypes.BOOL
            user32.GetClipboardData.argtypes = [wintypes.UINT]
            user32.GetClipboardData.restype = wintypes.HANDLE
            user32.CloseClipboard.argtypes = []
            user32.CloseClipboard.restype = wintypes.BOOL
            kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
            kernel32.GlobalLock.restype = wintypes.LPVOID
            kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
            kernel32.GlobalUnlock.restype = wintypes.BOOL
            for _ in range(10):
                if user32.OpenClipboard(None):
                    break
                time.sleep(0.05)
            else:
                return ""
            try:
                handle = user32.GetClipboardData(13)   # CF_UNICODETEXT
                if not handle:
                    return ""
                ptr = kernel32.GlobalLock(handle)
                if not ptr:
                    return ""
                try:
                    return ctypes.wstring_at(ptr)
                finally:
                    kernel32.GlobalUnlock(handle)
            finally:
                user32.CloseClipboard()
        import tkinter

        root = tkinter.Tk()
        root.withdraw()
        try:
            return root.clipboard_get()
        finally:
            root.destroy()
    except Exception:
        return ""


def setup_deepl_key(arg_key=None):
    """A DeepL-kulcs beállítása a konzolon: a vágólapról (Enterrel jóváhagyva), parancssori
    paraméterből vagy beillesztéssel. Ellenőrzi a DeepL-nél, és csak működő kulcsot ment el."""
    from .translate import deepl_usage

    script = "rksubby.py"
    say("DeepL API-kulcs beállítása")
    key = extract_deepl_key(arg_key) if arg_key else ""
    try:
        if not key:
            clip = extract_deepl_key(read_clipboard_text(), strict=True)
            if clip:
                ans = input(f"  A vágólapon ezt a DeepL-kulcsot találtam: {mask_key(clip)}\n"
                            f"  Ezt mentsem el? [Enter = igen, n = nem]: ").strip().lower()
                if ans in ("", "i", "igen", "y", "yes"):
                    key = clip
        if not key:
            say("  A kulcs helye: https://www.deepl.com/your-account/keys (másolás ikon a kulcs mellett)")
            raw = input("  Illeszd be a kulcsot (jobb klikk vagy Ctrl+V), majd Enter: ")
            key = extract_deepl_key(raw)
            if not key:
                say("Nem találtam kulcsot a beillesztett szövegben. Másold ki újra a DeepL oldalán "
                    f"(másolás ikon), és futtasd: python {script} --deepl-key")
                return 1
    except (EOFError, KeyboardInterrupt):
        say("\nMegszakítva.")
        return 1
    say(f"Kulcs: {mask_key(key)} ({'Free' if key.endswith(':fx') else 'Pro'}). Ellenőrzöm a DeepL-nél...")
    try:
        used, limit = deepl_usage(key)
    except Exception as e:
        say(f"A kulcs nem működik: {short_err(e)}")
        return 1
    save_deepl_key(key)
    say("Rendben, a kulcs működik, elmentettem.")
    say(f"Havi keret: {fmt_int(used)} / {fmt_int(limit)} karakter felhasználva.")
    if os.environ.get("DEEPL_API_KEY", "").strip() and os.environ["DEEPL_API_KEY"].strip() != key:
        say("Figyelem: a DEEPL_API_KEY környezeti változó is be van állítva, és az elsőbbséget élvez.")
    return 0


def test_translators(cfg):
    """--test-translate: minden fordítót kipróbál egy mondaton (Whisper nélkül, pár másodperc)."""
    from .config import load_profile
    from .translate import test_one

    sample = "Get in the car, Franklin, before the cops get here!"
    say(f"Fordítók tesztje: \"{sample}\"\n")
    key = load_deepl_key()
    for k in ("deepl", "ollama", "google", "google_web"):
        r = test_one(k, cfg, load_profile(cfg.profile), key, sample)
        if r.get("usage"):
            say(f"  deepl       : havi keret {fmt_int(r['usage']['used'])} / {fmt_int(r['usage']['limit'])} karakter")
        if r["ok"]:
            say(f"  {r['name']:<12}: OK   {r['ms']:5d} ms  -> {r['text']}")
        else:
            say(f"  {r['name']:<12}: HIBA -> {r['error']}")


# -----------------------------------------------------------------------------

def main(argv=None):
    windowless = _log_to_file()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description=f"rksubbyV2 {__version__} - élő magyar feliratozó "
                                             "(capture kártya / OBS, faster-whisper + DeepL)")
    ap.add_argument("--cli", action="store_true", help="konzolos mód: ablak nélkül, rögtön élőben indul")
    ap.add_argument("--start", action="store_true", help="rögtön élőben indul (a lejátszóval együtt)")
    ap.add_argument("--setup", action="store_true", help="a beállító ablak nyíljon meg (akkor is, ha már kész)")
    ap.add_argument("--no-window", action="store_true", help="ne nyisson ablakot (pl. csak OBS-hez)")
    ap.add_argument("--card", help='capture kártya DirectShow-neve hangforrásnak (pl. "AVerMedia HD Capture GC573 1")')
    ap.add_argument("--port", type=int, help="a webszerver portja (alap: a beállításokból, 8765)")
    ap.add_argument("--list-devices", action="store_true", help="bemeneti eszközök listája")
    ap.add_argument("--device", help="bemeneti eszköz neve (részlet) vagy sorszáma")
    ap.add_argument("--wav", help="hangfájl lejátszása valós időben élő bemenet helyett (konzolos mód)")
    ap.add_argument("--speed", type=float, default=1.0, help="WAV lejátszási sebesség")
    ap.add_argument("--model", help="Whisper modell (pl. distil-large-v3, large-v3-turbo)")
    ap.add_argument("--translator", choices=["deepl", "ollama", "auto", "google", "google_web", "none"],
                    help="fordító")
    ap.add_argument("--deepl-key", nargs="?", const="", default=None, metavar="KULCS",
                    help="DeepL API-kulcs beállítása a konzolon (a vágólapról is beolvassa)")
    ap.add_argument("--test-translate", action="store_true", help="a fordítók gyors tesztje, Whisper nélkül")
    ap.add_argument("--overlay", action="store_true", help="beépített, átlátszó felirat-ablak is (PyQt5)")
    ap.add_argument("--screen", type=int, help="melyik monitoron legyen az overlay (0, 1, 2...)")
    ap.add_argument("--no-overlay", action="store_true", help="overlay nélkül (ez az alapértelmezés)")
    ap.add_argument("--debug", action="store_true", help="beszédszakaszok kiírása")
    args = ap.parse_args(argv)

    cfg = Config()
    if cfg.load_error:
        say(f"[figyelem] {cfg.load_error}")
    if args.device:
        cfg.set_override("audio_kind", "pa")
        if args.device.isdigit():
            cfg.set_override("device_index", int(args.device))
        else:
            cfg.set_override("device_name", args.device)
            cfg.set_override("device_index", None)
    if args.card:
        cfg.set_override("audio_kind", "dshow")
        cfg.set_override("dshow_device", args.card)
    if args.overlay and not args.no_overlay:
        cfg.set_override("screen_overlay", True)
    if args.model:
        cfg.set_override("model", args.model)
        cfg.set_override("cpu_model", args.model)
    if args.translator:
        cfg.set_override("translator", args.translator)
    if args.screen is not None:
        cfg.set_override("overlay_screen", args.screen)

    if args.list_devices:
        from .audio import print_devices
        from .capture import find_ffmpeg, list_dshow_devices

        print_devices()
        cards = [d["name"] for d in list_dshow_devices(find_ffmpeg(cfg)) if {"audio", "video"} <= d["types"]]
        if cards:
            print('Capture kártyák (kép + hang; használat: --card "név"):')
            for c in cards:
                print(f"       {c}")
        return 0
    if args.deepl_key is not None:
        return setup_deepl_key(args.deepl_key or None)
    if args.test_translate:
        test_translators(cfg)
        return 0

    cli_mode = args.cli or bool(args.wav)
    port = args.port or cfg.http_port
    app_url = f"http://127.0.0.1:{port}/app"
    say(f"rksubbyV2 {__version__}")
    player_url = f"http://127.0.0.1:{port}/player"
    to_player = cfg.setup_done and cfg.launch_player and not args.setup
    if not cli_mode and other_instance(port):
        say("Már fut egy példány: megnyitom az ablakát.")
        open_window(player_url if to_player else app_url, size=(1280, 760) if to_player else (1240, 880))
        return 0

    from .api import Api
    from .display import SubtitleSink
    from .engine import Engine
    from .server import SubtitleServer

    sink = SubtitleSink(cfg, echo=say)
    engine = Engine(cfg, sink, log=say, debug=args.debug)
    quit_event = threading.Event()
    api = Api(cfg, sink, engine, port, quit_event, log=say)
    server = SubtitleServer(sink, port, api)
    if server.start():
        say(f"OBS-felirat: http://127.0.0.1:{port}/  (böngészőforrás, 'Helyi fájl' kikapcsolva, 1920x1080)")
    else:
        say(f"[figyelem] A webszerver nem indult (port {port}: {server.error}). Fut még egy példány?")
        if not cli_mode:
            say("A grafikus felület enélkül nem működik. Állíts be másik portot: python rksubby.py --port 8766")
            return 1

    rc = 0
    if cli_mode:
        if cfg.translator in ("deepl", "auto") and not load_deepl_key():
            interactive = False
            try:
                interactive = sys.stdin is not None and sys.stdin.isatty()
            except Exception:
                pass
            if interactive:
                say("Még nincs beállítva DeepL-kulcs.\n")
                if setup_deepl_key() != 0:
                    say("Kulcs nélkül megy tovább: angol felirat jelenik meg. Később: python rksubby.py --deepl-key\n")
        engine.start("test" if args.wav else "live", args.wav, args.speed)
        say("Kilépés: Ctrl+C\n")

        def watch_engine():   # konzolos módban a motor leállása (próba vége, hiba) a program vége is
            nonlocal rc
            while not quit_event.is_set():
                if engine.state == "error":
                    rc = 1
                    quit_event.set()
                elif engine.state == "idle":
                    quit_event.set()
                time.sleep(0.2)

        threading.Thread(target=watch_engine, daemon=True).start()
    else:
        say(f"Beállítás és vezérlés: {app_url}")
        say(f"Lejátszó: {player_url}")
        if cfg.open_window and not args.no_window:
            if to_player:
                open_window(player_url, size=(1280, 760))
            else:
                open_window(app_url)
        if args.start or (to_player and cfg.autostart):
            engine.start("live")
        if windowless and cfg.open_window and not args.no_window:
            threading.Thread(target=_quit_without_windows, args=(api, quit_event), daemon=True).start()
        say("Kilépés: az ablak Kilépés gombja, vagy itt Ctrl+C\n")

    # az átlátszó felirat-réteg külön folyamat (a felületről menet közben is ki-be kapcsolható)
    from .overlay_qt import OverlayProcess

    overlay = OverlayProcess(cfg, port, log=say)
    try:
        while not quit_event.wait(0.3):
            pass
    except KeyboardInterrupt:
        pass
    say("Kilépés...")
    overlay.stop()
    engine.stop(reason="Leállítva.")
    tr_stats = engine.last_stats
    sink.clear()
    sink.close()
    server.stop()
    if tr_stats and tr_stats["sent_chars"]:
        say(f"Fordítás ebben a menetben: {fmt_int(tr_stats['sent_chars'])} karakter elküldve, "
            f"ebből előre fordítás és el nem használt: ~{tr_stats['waste_pct']}%")
    return rc
