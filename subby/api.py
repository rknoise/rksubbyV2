# -*- coding: utf-8 -*-
"""A grafikus felület (/app) hívásai: állapot, beállítások, ellenőrzés, DeepL-kulcs, profilok,
indítás/leállítás, hangszintmérő, csomagtelepítés. A modul csak stdlib-et tölt be."""

import importlib.util
import os
import statistics
import threading
import time

from . import __version__
from .config import (BASE_DIR, CONFIG_PATH, DEFAULTS, LIVE_KEYS, SRT_DIR, TRANSLATORS, deepl_key_source,
                     list_profiles, load_deepl_key, load_profile, profile_id_from_name, reset_profile,
                     save_deepl_key, save_profile, valid_profile_id)
from .display import LOG_PATH
from .setup_check import Installer, run_checks
from .textutil import extract_deepl_key, mask_key, short_err

_DISPLAY_KEYS = {"style", "max_lines", "show_en_under_hu", "show_partial_en"}


def _median(xs):
    return round(statistics.median(xs)) if xs else None


def _base_ok():
    return all(importlib.util.find_spec(m) is not None
               for m in ("faster_whisper", "sounddevice", "numpy", "requests", "onnxruntime"))


class Api:
    def __init__(self, cfg, sink, engine, port, quit_event, log=print):
        self.cfg, self.sink, self.engine = cfg, sink, engine
        self.port, self.quit_event, self.log = port, quit_event, log
        self.installer = Installer()
        self.meter = None
        self.srt_path = ""
        self._last_poll = time.monotonic()
        self.last_ui = 0.0               # mikor jelentkezett utoljára egy ablak (vezérlő vagy lejátszó)
        self._checks, self._checks_t = None, 0.0
        self._checks_lock = threading.Lock()
        self._deepl_usage = None
        self._player = None
        engine.on_before_start = self._stop_meter
        threading.Thread(target=self._meter_guard, daemon=True, name="mero-or").start()

    # ------------------------------------------------------------ GET
    def get(self, name, q):
        if name == "status":
            return 200, self.status()
        if name == "config":
            return 200, self.config_payload()
        if name == "checks":
            return 200, self.checks(refresh=q.get("refresh") == "1")
        if name == "devices":
            return 200, self.devices()
        if name == "profile":
            return 200, load_profile(q.get("id") or self.cfg.profile)
        return 404, {"error": "ismeretlen kérés"}

    def status(self):
        self._last_poll = self.last_ui = time.monotonic()
        hist = self.sink.history_list(30)
        stats = {"lines": len(hist),
                 "lat_med": _median([h["lat_ms"] for h in hist]),
                 "asr_med": _median([h["asr_ms"] for h in hist]),
                 "tr_med": _median([h["tr_ms"] for h in hist if h["tr_ms"] >= 0])}
        return {"version": __version__, "engine": self.engine.status(), "history": hist, "stats": stats,
                "paused": self.sink.paused, "srt": self.srt_path, "install": self.installer.status(),
                "meter": self.meter.level() if self.meter and self.meter.active else None,
                "player": self._player_info(), "setup_done": self.cfg.setup_done}

    def _player_info(self):
        p = self._player
        if not p:
            return None
        return dict(p, open=time.monotonic() - p["t"] < 12, t=None)

    def config_payload(self):
        key = load_deepl_key()
        return {"config": self.cfg.as_dict(), "profiles": list_profiles(), "profile": load_profile(self.cfg.profile),
                "translators": list(TRANSLATORS),
                "deepl": {"has_key": bool(key), "masked": mask_key(key) if key else "", "free": key.endswith(":fx"),
                          "source": deepl_key_source(), "usage": self._deepl_usage},
                "paths": {"config": CONFIG_PATH, "log": LOG_PATH, "srt_dir": SRT_DIR, "base": BASE_DIR},
                "obs_url": f"http://127.0.0.1:{self.port}/", "version": __version__,
                "default_style": DEFAULTS["style"], "base_ok": _base_ok()}

    def checks(self, refresh=False):
        with self._checks_lock:
            if self._checks is None or refresh or time.monotonic() - self._checks_t > 60:
                try:
                    self._checks = run_checks(self.cfg)
                except Exception as e:
                    self._checks = {"error": short_err(e)}
                self._checks_t = time.monotonic()
            return self._checks

    def devices(self):
        """A választható hangforrások (capture kártyák + Windows-bemenetek) és a kimeneti eszközök."""
        if importlib.util.find_spec("sounddevice") is None or importlib.util.find_spec("numpy") is None:
            return {"sources": [], "outputs": [], "errors": ["előbb telepítsd a csomagokat (1. lépés)"]}
        from .capture import list_audio_sources, list_output_devices

        sources, errors = list_audio_sources(self.cfg)
        try:
            outputs = list_output_devices()
        except Exception as e:
            outputs, errors = [], errors + [short_err(e)]
        return {"sources": sources, "outputs": outputs, "errors": errors}

    # ------------------------------------------------------------ POST
    def post(self, name, body):
        fn = {
            "config": self.p_config,
            "deepl/key": self.p_deepl_key,
            "deepl/usage": self.p_deepl_usage,
            "translate/test": self.p_translate_test,
            "profile/save": self.p_profile_save,
            "profile/reset": self.p_profile_reset,
            "engine/start": self.p_engine_start,
            "engine/stop": self.p_engine_stop,
            "subs/clear": self.p_subs_clear,
            "subs/pause": self.p_subs_pause,
            "srt": self.p_srt,
            "meter": self.p_meter,
            "player/open": self.p_player_open,
            "player/hello": self.p_player_hello,
            "app/open": self.p_app_open,
            "install": self.p_install,
            "open": self.p_open,
            "quit": self.p_quit,
        }.get(name)
        if fn is None:
            return 404, {"error": "ismeretlen kérés"}
        return fn(body)

    def p_config(self, body):
        changed = self.cfg.update(body)
        if changed:
            self.cfg.save()
            if _DISPLAY_KEYS & set(changed):
                self.sink.touch()
        running = self.engine.state in ("loading", "running")
        restart = running and any(k not in LIVE_KEYS for k in changed)
        return 200, {"changed": changed, "restart_needed": restart, "config": self.cfg.as_dict()}

    def p_deepl_key(self, body):
        key = extract_deepl_key(str(body.get("key") or ""))
        if not key:
            return 400, {"error": "Nem találtam kulcsot a beillesztett szövegben. Másold ki újra a DeepL "
                                  "oldalán (másolás ikon a kulcs mellett)."}
        if importlib.util.find_spec("requests") is None:
            return 400, {"error": "Előbb telepítsd a csomagokat (1. lépés)."}
        from .translate import deepl_usage

        try:
            used, limit = deepl_usage(key)
        except Exception as e:
            return 400, {"error": f"A kulcs nem működik: {short_err(e)}"}
        save_deepl_key(key)
        self._deepl_usage = {"used": used, "limit": limit, "t": time.strftime("%H:%M")}
        warn = ""
        if deepl_key_source() == "env" and os.environ.get("DEEPL_API_KEY", "").strip() != key:
            warn = "A DEEPL_API_KEY környezeti változó is be van állítva, és az elsőbbséget élvez."
        return 200, {"ok": True, "masked": mask_key(key), "free": key.endswith(":fx"),
                     "usage": self._deepl_usage, "warning": warn}

    def p_deepl_usage(self, body):
        key = load_deepl_key()
        if not key:
            return 400, {"error": "nincs DeepL-kulcs"}
        from .translate import deepl_usage

        try:
            used, limit = deepl_usage(key)
        except Exception as e:
            return 400, {"error": short_err(e)}
        self._deepl_usage = {"used": used, "limit": limit, "t": time.strftime("%H:%M")}
        return 200, {"usage": self._deepl_usage}

    def p_translate_test(self, body):
        if importlib.util.find_spec("requests") is None:
            return 400, {"error": "Előbb telepítsd a csomagokat (1. lépés)."}
        from .translate import test_one

        kind = body.get("translator") or self.cfg.translator
        if kind == "auto":
            kind = "deepl"
        if kind not in TRANSLATORS:
            return 400, {"error": "ismeretlen fordító"}
        text = str(body.get("text") or "").strip()[:300] or "Get in the car, Franklin, before the cops get here!"
        res = test_one(kind, self.cfg, load_profile(self.cfg.profile), load_deepl_key(), text)
        if res.get("usage"):
            self._deepl_usage = dict(res["usage"], t=time.strftime("%H:%M"))
        return 200, res

    def p_profile_save(self, body):
        name = str(body.get("name") or "").strip()
        pid = str(body.get("id") or "")
        if body.get("new") or not valid_profile_id(pid):
            if not name:
                return 400, {"error": "adj nevet a profilnak"}
            base = pid = profile_id_from_name(name)
            existing = {p["id"] for p in list_profiles()}
            n = 2
            while pid in existing:
                pid, n = f"{base}-{n}", n + 1
        data = {"name": name or pid, "hotwords": body.get("hotwords", ""), "llm_hint": body.get("llm_hint", ""),
                "glossary": body.get("glossary") or []}
        try:
            prof = save_profile(pid, data)
        except (OSError, ValueError) as e:
            return 400, {"error": short_err(e)}
        changed = self.cfg.update({"profile": pid}) if body.get("select", True) else []
        if changed:
            self.cfg.save()
        restart = self.engine.state in ("loading", "running") and (pid == self.cfg.profile)
        return 200, {"profile": prof, "profiles": list_profiles(), "restart_needed": restart}

    def p_profile_reset(self, body):
        pid = str(body.get("id") or "")
        reset_profile(pid)
        profiles = list_profiles()
        if not any(p["id"] == pid for p in profiles):     # saját profil volt: törlődött
            self.cfg.update({"profile": "altalanos"})
            self.cfg.save()
            pid = "altalanos"
        return 200, {"profile": load_profile(pid), "profiles": profiles, "config": self.cfg.as_dict()}

    def p_engine_start(self, body):
        if not _base_ok():
            return 400, {"error": "Előbb telepítsd a csomagokat (1. lépés)."}
        mode = "test" if body.get("mode") == "test" else "live"
        ok, msg = self.engine.start(mode)
        return (200 if ok else 409), {"ok": ok, "message": msg}

    def p_engine_stop(self, body):
        threading.Thread(target=self.engine.stop, daemon=True).start()   # ne tartsa fel a választ
        return 200, {"ok": True}

    def p_subs_clear(self, body):
        self.sink.clear()
        return 200, {"ok": True}

    def p_subs_pause(self, body):
        self.sink.set_paused(bool(body.get("paused")))
        return 200, {"paused": self.sink.paused}

    def p_srt(self, body):
        if body.get("on"):
            try:
                self.srt_path = self.sink.start_srt()
            except OSError as e:
                return 400, {"error": f"az SRT-fájl nem hozható létre: {e}"}
        else:
            self.sink.stop_srt()
            self.srt_path = ""
        return 200, {"srt": self.srt_path}

    def p_meter(self, body):
        """A beállított hangforrás mérése (és lejátszása), amíg a varázsló 2. lépésén vagy."""
        if not body.get("on"):
            self._stop_meter()
            return 200, {"meter": None}
        if self.engine.state in ("loading", "running"):
            return 409, {"error": "fut a feliratozó: a hangszintet a vezérlőpulton látod"}
        if importlib.util.find_spec("sounddevice") is None or importlib.util.find_spec("numpy") is None:
            return 400, {"error": "előbb telepítsd a csomagokat (1. lépés)"}
        from .capture import LevelMeter

        if self.meter is None:
            self.meter = LevelMeter(self.cfg, log=self.log)
        self._last_poll = time.monotonic()
        self.meter.start()
        return 200, {"meter": self.meter.level()}

    def p_player_open(self, body):
        """A lejátszóablak megnyitása; ha kérik, a feliratozás is indul."""
        from .windows import open_window

        self._stop_meter()
        open_window(f"http://127.0.0.1:{self.port}/player", size=(1280, 760), fullscreen=bool(body.get("fullscreen")))
        started = False
        if body.get("start") and _base_ok() and self.engine.state in ("idle", "error"):
            started, _msg = self.engine.start("live")
        return 200, {"ok": True, "started": started}

    def p_player_hello(self, body):
        """A lejátszó időnként jelentkezik: ilyenkor (újra) mindig legfelülre tesszük, ha kell."""
        from .windows import PLAYER_TITLE, set_topmost

        self.last_ui = time.monotonic()
        devices = body.get("devices")
        self._player = {"t": time.monotonic(), "video": str(body.get("video") or ""),
                        "res": str(body.get("res") or ""), "error": str(body.get("error") or ""),
                        "devices": [str(d) for d in devices][:20] if isinstance(devices, list) else [],
                        "trace": [str(x)[:300] for x in body.get("trace") or []][-12:]}
        found = set_topmost(PLAYER_TITLE, bool(self.cfg.player_topmost))
        return 200, {"topmost": bool(self.cfg.player_topmost), "found": found}

    def p_app_open(self, body):
        from .windows import open_window

        view = str(body.get("view") or "")
        open_window(f"http://127.0.0.1:{self.port}/app" + (f"#{view}" if view.isalnum() else ""))
        return 200, {"ok": True}

    def p_install(self, body):
        ok, msg = self.installer.start(str(body.get("what") or ""))
        self._checks = None
        return (200 if ok else 409), {"ok": ok, "message": msg}

    def p_open(self, body):
        target = {"log": os.path.dirname(LOG_PATH), "srt": SRT_DIR, "config": os.path.dirname(CONFIG_PATH),
                  "base": BASE_DIR}.get(body.get("what"))
        if not target or not hasattr(os, "startfile"):
            return 400, {"error": "nem nyitható meg"}
        os.makedirs(target, exist_ok=True)
        os.startfile(target)   # noqa: S606 - csak a fenti, rögzített mappák egyike
        return 200, {"ok": True}

    def p_quit(self, body):
        self.quit_event.set()
        return 200, {"ok": True}

    # ------------------------------------------------------------ hangszintmérő
    def _stop_meter(self):
        if self.meter:
            self.meter.stop()

    def _meter_guard(self):
        """Ha a felületet bezárták (nem kérdez le állapotot), a mérő ne tartsa nyitva az eszközt."""
        while True:
            time.sleep(2.0)
            if self.meter and self.meter.active and time.monotonic() - self._last_poll > 10:
                self.meter.stop()
