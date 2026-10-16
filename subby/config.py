# -*- coding: utf-8 -*-
"""Beállítások: alapértékek, a felhasználói config.json, a DeepL-kulcs és a játékprofilok.

A beállítások a felhasználói mappába kerülnek (%USERPROFILE%\\.rksubby\\config.json), NEM a
projektmappába, így egy új verzió letöltésekor nem vesznek el, és nem adod tovább véletlenül.
Csak a stdlib-et használja: a grafikus felület a csomagok telepítése előtt is elindul.
"""

import copy
import json
import os
import re
import threading

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # a projektmappa
HOME_DIR = os.environ.get("RKSUBBY_HOME") or os.path.join(os.path.expanduser("~"), ".rksubby")
CONFIG_PATH = os.path.join(HOME_DIR, "config.json")
DEEPL_KEY_FILE = os.path.join(HOME_DIR, "deepl_key.txt")
USER_PROFILE_DIR = os.path.join(HOME_DIR, "profiles")
BUILTIN_PROFILE_DIR = os.path.join(BASE_DIR, "profiles")
SRT_DIR = os.path.join(BASE_DIR, "feliratok")
TEST_WAV = os.path.join(BASE_DIR, "teszt_angol.wav")
SAMPLE_RATE = 16000              # a Whisper ennyit vár

DEFAULTS = {
    # --- Forrás: kép és hang ---
    "audio_kind": "pa",              # "dshow": capture kártya hangja ffmpeg-gel (pl. AVerMedia GC573),
                                     # "pa": Windows-hangbemenet (VB-CABLE, USB-s kártya, mikrofon)
    "dshow_device": "",              # a capture kártya DirectShow-neve (pl. "AVerMedia HD Capture GC573 1")
    "device_name": "CABLE Output",   # "pa" hangbemenet neve (részlet); a sorszám eszközváltáskor elcsúszhat
    "device_index": None,            # ha mégis sorszámot szeretnél (pl. 12)
    "video_device": "",              # a lejátszó képforrása (a böngésző szerinti név)
    "video_res": "1920x1080",
    "video_fps": 60,
    "monitor_audio": True,           # a forrás hangja szóljon ezen a gépen is
    "monitor_volume": 0.8,
    "monitor_device": "",            # kimeneti eszköz (üres = a Windows alapértelmezett)
    "ffmpeg_path": "",               # üres = a telepítővel érkezett (imageio-ffmpeg), különben a PATH-ból

    # --- Beszédfelismerés ---
    "model": "distil-large-v3",      # angolra gyors és pontos; alternatíva: "large-v3-turbo"
    "cpu_model": "small.en",         # ha nincs használható NVIDIA GPU: ezzel fut CPU-n
    "asr_device": "auto",            # "auto" | "cuda" | "cpu"
    "final_beam_size": 5,            # kész mondatnál (3090-en alig lassabb, mint az 1)
    "partial_beam_size": 1,          # élő előnézetnél
    "use_context_prompt": True,      # az előző mondat(ok) segítik a felismerést
    "context_reset_s": 20.0,         # ennyi csend után új jelenetnek veszi, törli a kontextust

    # --- Játékprofil (nevek, glosszárium, LLM-utasítás): profiles\*.json ---
    "profile": "gta5",

    # --- Beszédszakaszok (Silero VAD) ---
    "vad_threshold": 0.5,            # efölött beszéd
    "vad_neg_threshold": 0.35,       # ez alatt csend (a kettő között marad az előző állapot)
    "start_speech_ms": 160,          # ennyi folyamatos beszéd indít egy szakaszt
    "min_utterance_speech_ms": 250,  # ennél kevesebb beszéd zaj (lövés, ajtócsapás): eldobjuk
    "end_silence_ms": 450,           # ennyi csend után lezárjuk a mondatot
    "speculative_silence_ms": 250,   # ekkor már előre felismer (és fordít); 0 = kikapcsolva
    "smart_endpoint": True,          # ha ekkor a szöveg mondatvégi írásjellel zárul, rögtön lezárja
    "pre_roll_ms": 300,              # a beszéd eleje elől ennyi hangot is hozzáveszünk
    "post_roll_ms": 200,             # a beszéd vége után ennyi csend marad a darabban
    "max_utterance_s": 12.0,         # biztosíték: ennél hosszabb szakaszt a legcsendesebb ponton vágjuk

    # --- Élő előnézet és korai lezárás ---
    "partial_interval_s": 0.5,       # milyen gyakran nézzen bele a folyamatban lévő mondatba (0 = ki)
    "early_commit": True,            # hosszú beszédnél a kész mondatokat rögtön kiküldi
    "early_commit_min_s": 2.5,       # csak ennél hosszabb szakasznál próbálkozik
    "early_commit_guard_s": 0.6,     # a mondatvég után legalább ennyi hang kell már

    # --- Fordítás ---
    "translator": "deepl",           # "deepl" | "ollama" | "google" | "google_web" | "none"
    "deepl_formality": "prefer_less",   # tegező ("prefer_less") vagy magázó ("prefer_more") hangnem
    "deepl_glossary": True,          # a profil glosszáriumát DeepL-glosszáriumként is használja
    "spec_translate": True,          # előre fordítás a mondat lezárása előtt (gyorsabb, kicsit több karakter)
    "translate_fallback": True,      # ha a választott fordító nem elérhető: helyi AI, végül Google (így magyar marad)
    "ollama_url": "http://localhost:11434",
    "ollama_model": "translategemma:12b",   # vagy pl. "gemma3:12b" (az kontextust is kap)
    "context_lines": 3,              # ennyi előző mondat megy kontextusnak (DeepL / LLM)
    "translate_timeout_s": 6.0,

    # --- Megjelenítés ---
    "player_topmost": True,          # a lejátszóablak mindig legfelül
    "screen_overlay": False,         # átlátszó felirat-réteg a képernyő alján (OBS és lejátszó nélkül)
    "http_port": 8765,               # a helyi webszerver; OBS böngészőforrás URL-je: http://127.0.0.1:8765/
    "write_sub_files": False,        # sub.txt + sub.json is (csak a régi, fájlos sub.html-hez kell)
    "show_partial_en": False,        # beszéd közben szürke angol előnézet
    "show_en_under_hu": False,       # a magyar alatt kicsiben az angol eredeti is
    "max_lines": 2,                  # egyszerre ennyi mondat látszik
    "sub_min_s": 1.8,                # egy felirat legalább ennyi ideig látszik
    "sub_max_s": 7.0,
    "read_chars_per_s": 14,          # olvasási tempó a kijelzési időhöz
    "style": {
        "font": "Arial",
        "font_size": 32,             # pixel (1920x1080-as böngészőforráson)
        "color": "#ffff00",
        "en_color": "#dddddd",
        "box": False,                # félig átlátszó háttérdoboz a szöveg mögött
        "box_opacity": 0.5,
        "max_width": 1300,           # ennél szélesebb sor nem lesz (px), így könnyebb olvasni
        "bottom": 40,                # távolság a kép aljától (px)
    },
    "overlay_screen": 0,             # a beépített Qt-ablak (--overlay) melyik monitoron legyen
    "log_enabled": True,             # sub_log.tsv: EN / HU / késés mondatonként

    # --- Grafikus felület ---
    "setup_done": False,             # végigment-e már a beállítóvarázslón
    "open_window": True,             # induláskor megnyit-e ablakot (beállítás vagy lejátszó)
    "launch_player": True,           # a beállítás után induláskor rögtön a lejátszó nyílik meg
    "autostart": True,               # ... és a feliratozás is magától elindul
}

# Ezek a futó feliratozót is azonnal módosítják; a többihez újraindítás kell
LIVE_KEYS = {
    "show_partial_en", "show_en_under_hu", "max_lines", "sub_min_s", "sub_max_s", "read_chars_per_s",
    "style", "write_sub_files", "setup_done", "open_window", "deepl_formality", "log_enabled",
    "overlay_screen", "http_port", "video_device", "video_res", "video_fps", "monitor_audio",
    "monitor_volume", "player_topmost", "screen_overlay", "launch_player", "autostart", "ffmpeg_path",
    # a fordító futás közben cserélhető (Engine.reload_translator)
    "translator", "translate_fallback", "ollama_url", "ollama_model", "deepl_glossary",
}
TRANSLATOR_KEYS = {"translator", "translate_fallback", "ollama_url", "ollama_model", "deepl_glossary"}

TRANSLATORS = ("deepl", "ollama", "google", "google_web", "none")
_CHOICES = {
    "audio_kind": ("pa", "dshow"),
    "video_res": ("1920x1080", "1280x720", "2560x1440", "3840x2160"),
    "translator": TRANSLATORS + ("auto",),
    "asr_device": ("auto", "cuda", "cpu"),
    "deepl_formality": ("prefer_less", "prefer_more", "default", ""),
}
_COLOR_RE = re.compile(r"#[0-9a-fA-F]{6}")


def _coerce(default, value):
    """Az új értéket az alapérték típusára alakítja (ValueError/TypeError, ha nem megy)."""
    if isinstance(default, bool):
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "igen", "yes", "on")
        return bool(value)
    if isinstance(default, int):
        return int(round(float(value)))
    if isinstance(default, float):
        return float(value)
    if isinstance(default, str):
        return "" if value is None else str(value)
    return value


class Config:
    """A beállítások. Attribútumként olvasható (cfg.end_silence_ms); a parancssori felülírások
    (pl. --model) érvényesek, de nem kerülnek a config.json-ba."""

    def __init__(self, path=CONFIG_PATH, load=True):
        self.path = path
        self._lock = threading.RLock()
        self._data = copy.deepcopy(DEFAULTS)
        self._overrides = {}
        self.load_error = ""
        if load:
            self.load()

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        if name in self._overrides:
            return self._overrides[name]
        try:
            return self._data[name]
        except KeyError:
            raise AttributeError(name) from None

    def as_dict(self):
        with self._lock:
            d = copy.deepcopy(self._data)
            d.update(copy.deepcopy(self._overrides))
            return d

    def set_override(self, key, value):
        if key not in DEFAULTS:
            raise KeyError(key)
        self._overrides[key] = value

    def update(self, changes):
        """Ismert kulcsok érvényes értékeit átveszi; visszaadja a ténylegesen megváltozott kulcsokat."""
        changed = []
        with self._lock:
            for key, value in (changes or {}).items():
                if key not in DEFAULTS:
                    continue
                default = DEFAULTS[key]
                try:
                    if key == "style":
                        if not isinstance(value, dict):
                            continue
                        new = dict(self._data["style"])
                        for sk, sv in value.items():
                            if sk not in default:
                                continue
                            sv = _coerce(default[sk], sv)
                            if sk in ("color", "en_color") and not _COLOR_RE.fullmatch(sv):
                                continue
                            new[sk] = sv
                        new["font_size"] = max(12, min(120, new["font_size"]))
                        new["box_opacity"] = max(0.0, min(1.0, new["box_opacity"]))
                        new["max_width"] = max(400, min(1920, new["max_width"]))
                        new["bottom"] = max(0, min(600, new["bottom"]))
                        value = new
                    elif default is None:          # device_index: szám vagy None
                        value = None if value in (None, "") else int(value)
                    else:
                        value = _coerce(default, value)
                except (TypeError, ValueError):
                    continue
                if key in _CHOICES and value not in _CHOICES[key]:
                    continue
                if key == "max_lines":
                    value = max(1, min(4, value))
                if key == "monitor_volume":
                    value = max(0.0, min(1.5, value))
                if key == "video_fps":
                    value = max(10, min(240, value))
                if key == "profile" and not valid_profile_id(value):
                    continue
                self._overrides.pop(key, None)
                if self._data.get(key) != value:
                    self._data[key] = value
                    changed.append(key)
        return changed

    def load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return False
        except (OSError, ValueError) as e:
            self.load_error = f"a beállításfájl hibás, az alapértékekkel megyek tovább ({e})"
            return False
        if isinstance(data, dict):
            self.update(data)
        return True

    def save(self):
        with self._lock:
            data = copy.deepcopy(self._data)
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)


# -----------------------------------------------------------------------------
# DeepL-kulcs
# -----------------------------------------------------------------------------

def deepl_key_source():
    if os.environ.get("DEEPL_API_KEY", "").strip():
        return "env"
    return "file" if os.path.exists(DEEPL_KEY_FILE) else ""


def load_deepl_key():
    key = os.environ.get("DEEPL_API_KEY", "").strip()
    if key:
        return key
    try:
        with open(DEEPL_KEY_FILE, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def save_deepl_key(key):
    os.makedirs(os.path.dirname(DEEPL_KEY_FILE), exist_ok=True)
    with open(DEEPL_KEY_FILE, "w", encoding="utf-8") as f:
        f.write(key)


# -----------------------------------------------------------------------------
# Játékprofilok: a beépítettek a projekt profiles\ mappájában, a módosítottak és a sajátok a
# felhasználói mappában (ugyanazzal az azonosítóval felülírják a beépítettet)
# -----------------------------------------------------------------------------

_PROFILE_ID_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,39}")
_ACCENTS = str.maketrans("áéíóöőúüűÁÉÍÓÖŐÚÜŰ", "aeiooouuuAEIOOOUUU")


def valid_profile_id(pid):
    return isinstance(pid, str) and bool(_PROFILE_ID_RE.fullmatch(pid))


def profile_id_from_name(name):
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").translate(_ACCENTS).lower()).strip("-")
    return s[:40] or "sajat"


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def clean_glossary(entries):
    """[[angol, magyar], ...] -> tisztított lista: üres, tabos/sortöréses és ismétlődő sorok nélkül."""
    out, seen = [], set()
    for pair in entries or []:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            continue
        en, hu = (re.sub(r"\s+", " ", str(x)).strip() for x in pair)
        if not en or not hu or en.lower() in seen:
            continue
        seen.add(en.lower())
        out.append([en, hu])
    return out


def _normalize_profile(pid, data, builtin):
    return {
        "id": pid,
        "name": str(data.get("name") or pid),
        "hotwords": str(data.get("hotwords") or ""),
        "llm_hint": str(data.get("llm_hint") or ""),
        "glossary": clean_glossary(data.get("glossary")),
        "builtin": builtin,
    }


def list_profiles():
    """[{id, name, builtin, customized}] név szerint rendezve."""
    found = {}
    for folder, builtin in ((BUILTIN_PROFILE_DIR, True), (USER_PROFILE_DIR, False)):
        try:
            names = os.listdir(folder)
        except OSError:
            continue
        for fn in names:
            pid, ext = os.path.splitext(fn)
            if ext.lower() != ".json" or not valid_profile_id(pid):
                continue
            data = _read_json(os.path.join(folder, fn))
            if data is None:
                continue
            if pid in found:              # a felhasználói felülírja a beépítettet
                found[pid].update(name=str(data.get("name") or pid), customized=True)
            else:
                found[pid] = {"id": pid, "name": str(data.get("name") or pid),
                              "builtin": builtin, "customized": False}
    return sorted(found.values(), key=lambda p: (p["id"] != "altalanos", p["name"].lower()))


def load_profile(pid):
    """A profil adatai; ismeretlen azonosítónál üres, általános profil."""
    if valid_profile_id(pid):
        user = _read_json(os.path.join(USER_PROFILE_DIR, pid + ".json"))
        builtin = os.path.exists(os.path.join(BUILTIN_PROFILE_DIR, pid + ".json"))
        if user is not None:
            return _normalize_profile(pid, user, builtin)
        data = _read_json(os.path.join(BUILTIN_PROFILE_DIR, pid + ".json"))
        if data is not None:
            return _normalize_profile(pid, data, True)
    return _normalize_profile("altalanos", {"name": "Általános"}, True)


def save_profile(pid, data):
    if not valid_profile_id(pid):
        raise ValueError("érvénytelen profilazonosító")
    prof = _normalize_profile(pid, data, False)
    out = {k: prof[k] for k in ("name", "hotwords", "llm_hint", "glossary")}
    os.makedirs(USER_PROFILE_DIR, exist_ok=True)
    path = os.path.join(USER_PROFILE_DIR, pid + ".json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return load_profile(pid)


def reset_profile(pid):
    """A felhasználói változat törlése (beépítettnél visszaállnak az alapértékek)."""
    if not valid_profile_id(pid):
        return False
    try:
        os.remove(os.path.join(USER_PROFILE_DIR, pid + ".json"))
        return True
    except OSError:
        return False
