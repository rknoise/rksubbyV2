# -*- coding: utf-8 -*-
"""Ablakok: a beállító/vezérlő ablak és a lejátszó megnyitása (Edge "alkalmazás" módban, saját
profillal), valamint a lejátszó "mindig legfelül" tartása (Win32 API)."""

import os
import shutil
import subprocess
import webbrowser

from .config import HOME_DIR

PLAYER_TITLE = "rksubby lejátszó"     # a lejátszó oldal <title>-je; ez alapján találjuk meg az ablakát

# Saját böngészőprofil: a kamera-/kártyaengedély és az ablakméret megmarad, és a saját beállításaink
# (automatikus engedély a kártya képére, hang lejátszása kattintás nélkül) nem érintik a megszokott böngésződet.
_FLAGS = ["--no-first-run", "--no-default-browser-check", "--disable-features=Translate",
          "--use-fake-ui-for-media-stream", "--autoplay-policy=no-user-gesture-required",
          "--disable-background-timer-throttling"]


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


def open_window(url, size=(1240, 880), fullscreen=False):
    """Önálló ablak címsor és lapfülek nélkül. Ha nincs Edge/Chrome, a szokásos böngészőben nyílik meg."""
    exe = browser_exe()
    if exe:
        args = [exe, f"--user-data-dir={os.path.join(HOME_DIR, 'browser')}", *_FLAGS,
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
