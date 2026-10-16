# -*- coding: utf-8 -*-
"""Ablakok: a beállító/vezérlő ablak és a lejátszó megnyitása (Edge "alkalmazás" módban, saját
profillal), valamint a lejátszó "mindig legfelül" tartása (Win32 API)."""

import json
import os
import shutil
import subprocess
import time
import urllib.parse
import webbrowser

from .config import HOME_DIR

PLAYER_TITLE = "rksubby lejátszó"     # a lejátszó oldal <title>-je; ez alapján találjuk meg az ablakát

# Saját böngészőprofil: a kártyaengedély és az ablakméret megmarad, és nem érinti a megszokott böngésződet.
# (Csak támogatott kapcsolók: a többi miatt az Edge figyelmeztető sávot tesz az ablak tetejére.)
PROFILE_DIR = os.path.join(HOME_DIR, "browser")
_FLAGS = ["--no-first-run", "--no-default-browser-check", "--disable-features=Translate"]


def browser_exe():
    for name, rel in (("msedge", r"Microsoft\Edge\Application\msedge.exe"),
                      ("chrome", r"Google\Chrome\Application\chrome.exe")):
        found = shutil.which(name)
        if found:
            return found
        for env in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"):
            base = os.environ.get(env)
            if base and os.path.exists(os.path.join(base, rel)):
                return os.path.join(base, rel)
    return None


def _profile_in_use(profile_dir):
    """Fut-e már az Edge ezzel a profillal (a "lockfile"-t futás közben kizárólagosan nyitva tartja)."""
    lock = os.path.join(profile_dir, "lockfile")
    if not os.path.exists(lock):
        return False
    try:
        with open(lock, "a"):
            return False
    except OSError:
        return True


def allow_capture(profile_dir, url):
    """A saját profilban előre engedélyezi a képforrást a program címére, így az Edge nem kérdez rá.
    Csak akkor írja a beállításfájlt, ha az Edge épp nem fut ezzel a profillal."""
    parts = urllib.parse.urlsplit(url)
    pattern = f"{parts.scheme}://{parts.netloc},*"
    pref = os.path.join(profile_dir, "Default", "Preferences")
    if _profile_in_use(profile_dir):
        return False
    try:
        with open(pref, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        data = {}
    except (OSError, ValueError):
        return False
    ex = data.setdefault("profile", {}).setdefault("content_settings", {}).setdefault("exceptions", {})
    cam = ex.setdefault("media_stream_camera", {})
    if (cam.get(pattern) or {}).get("setting") == 1:
        return True
    # a Chromium-idő mikroszekundum 1601-01-01 óta
    cam[pattern] = {"last_modified": str(int((time.time() + 11644473600) * 1_000_000)), "setting": 1}
    try:
        os.makedirs(os.path.dirname(pref), exist_ok=True)
        with open(pref, "w", encoding="utf-8") as f:
            json.dump(data, f, separators=(",", ":"))
        return True
    except OSError:
        return False


def open_window(url, size=(1240, 880), fullscreen=False):
    """Önálló ablak címsor és lapfülek nélkül. Ha nincs Edge/Chrome, a szokásos böngészőben nyílik meg."""
    exe = browser_exe()
    if exe:
        allow_capture(PROFILE_DIR, url)
        args = [exe, f"--user-data-dir={PROFILE_DIR}", *_FLAGS,
                f"--app={url}", f"--window-size={size[0]},{size[1]}"]
        if fullscreen:
            args.append("--start-fullscreen")
        try:
            subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        except OSError:
            pass
    webbrowser.open(url)
    return False


def set_topmost(title_part, on=True):
    """A címe alapján megtalált ablak(ok) mindig legfelül (vagy vissza normálba). Visszaadja, hányat talált."""
    if os.name != "nt":
        return 0
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [enum_proc, wintypes.LPARAM]
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                    ctypes.c_int, ctypes.c_uint]
    found = []

    def cb(hwnd, _lparam):
        if user32.IsWindowVisible(hwnd):
            n = user32.GetWindowTextLengthW(hwnd)
            if n:
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, buf, n + 1)
                if title_part in buf.value:
                    found.append(hwnd)
        return True

    user32.EnumWindows(enum_proc(cb), 0)
    hwnd_topmost, hwnd_notopmost = wintypes.HWND(-1), wintypes.HWND(-2)
    swp = 0x0001 | 0x0002 | 0x0010     # SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE
    for hwnd in found:
        user32.SetWindowPos(hwnd, hwnd_topmost if on else hwnd_notopmost, 0, 0, 0, 0, swp)
    return len(found)
