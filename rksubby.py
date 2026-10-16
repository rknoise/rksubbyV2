# -*- coding: utf-8 -*-
"""
rksubbyV2 - élő magyar feliratozó angol beszédhez (Xbox / bármilyen HDMI-forrás + OBS)

Útvonal: capture kártya -> OBS (hangfigyelés) -> VB-CABLE -> faster-whisper (GPU) -> DeepL -> OBS-felirat

Mit csinál
  * Beszédszakaszokban dolgozik, nem fix blokkokban: a Silero VAD 32 ms-onként figyeli a beszédet,
    így a Whisper egész mondatokat kap, és nem vág bele a szavakba.
  * A mondat vége után tipikusan ~0,6-0,8 mp-en belül kint a magyar felirat: a csend elején már
    előre felismer és fordít; mondatvégi írásjelnél 250 ms szünet után lezár; folyamatos beszédnél
    a kész mondatokat menet közben kiküldi, a fordításukat előre elindítja.
  * DeepL API: az előző mondatokat kontextusként kapja, tegező hangnemet kér.
  * OBS-felirat beépített helyi webszerverről: böngészőforrás URL-je http://127.0.0.1:8765/
  * Indításkor lépésenként kiírja, hol tart; ha nincs felirat, időnként a hangszintet is
    ("NEM JÖN HANG" = nem érkezik hang a CABLE-ről).
  * Napló: sub_log.tsv (angol, magyar, felismerési/fordítási idő, késés mondatonként).

Használat
  python rksubby.py                      élő mód (CABLE Output -> OBS: http://127.0.0.1:8765/)
  python rksubby.py --wav teszt_angol.wav   próba egy felvétellel, Xbox nélkül
  python rksubby.py --deepl-key          DeepL API-kulcs beállítása (az első indításkor magától kéri)
  python rksubby.py --test-translate     a fordító gyors tesztje (Whisper nélkül)
  python rksubby.py --list-devices       bemeneti eszközök listája
  python rksubby.py --overlay            OBS nélkül: beépített, átlátszó felirat-ablak (--screen N)
  python rksubby.py --debug              beszédszakaszok kiírása a konzolra

Telepítés és OBS-beállítás: README.md
"""

import os
import sys


def _add_nvidia_dll_dirs():
    """A pip-pel telepített nvidia-* csomagok DLL-jei (Windows Store-os Pythonhoz)."""
    if os.name != "nt":
        return
    import site

    candidates = {p for p in sys.path if "site-packages" in p}
    try:
        candidates.update(site.getsitepackages())
        candidates.add(site.getusersitepackages())
    except Exception:
        pass
    for sp in candidates:
        nvidia_dir = os.path.join(sp, "nvidia")
        if not os.path.isdir(nvidia_dir):
            continue
        for root, _dirs, _files in os.walk(nvidia_dir):
            if os.path.basename(root).lower() == "bin":
                try:
                    os.add_dll_directory(root)
                except Exception:
                    pass
                os.environ["PATH"] = root + os.pathsep + os.environ.get("PATH", "")


_add_nvidia_dll_dirs()

import argparse
import collections
import concurrent.futures
import importlib
import json
import queue
import re
import signal
import threading
import time
import warnings
from dataclasses import dataclass, field

import numpy as np

# A ctranslate2 betölti a torch-ot, az pedig egy ártalmatlan figyelmeztetést ír ki a pynvml-ről
warnings.filterwarnings("ignore", message="The pynvml package is deprecated")
warnings.filterwarnings("ignore", message=r"urllib3 .* doesn't match a supported version")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))   # a kimeneti fájlok a script mellé kerülnek
SCRIPT_NAME = os.path.basename(os.path.abspath(__file__))   # az üzenetekben ezzel a névvel hivatkozik magára

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# =============================================================================
# KONFIGURÁCIÓ
# =============================================================================

# --- Hangbemenet ---
DEVICE_NAME = "CABLE Output"   # név alapján keres (a sorszám eszközváltáskor elcsúszhat)
DEVICE_INDEX = None            # ha mégis sorszámot szeretnél (pl. 12), írd ide
SAMPLE_RATE = 16000            # a Whisper ennyit vár

# --- Beszédfelismerés ---
MODEL_NAME = "distil-large-v3"   # angolra gyors és pontos; alternatíva: "large-v3-turbo"
ASR_DEVICE = "cuda"
COMPUTE_TYPE = "float16"
CPU_MODEL_NAME = "small.en"      # ha nincs használható NVIDIA GPU: ezzel fut CPU-n (lassabb, pontatlanabb)
LANGUAGE = "en"                  # fix nyelv: nincs nyelvfelismerés minden darabon
FINAL_BEAM_SIZE = 5              # kész mondatnál (3090-en alig lassabb, mint az 1)
PARTIAL_BEAM_SIZE = 1            # élő előnézetnél
# Nevek, helyek: segítik a felismerést. GTA VI-hoz pl. "Jason, Lucia, Vice City, Leonida"
GAME_HOTWORDS = "Franklin, Michael, Trevor, Lamar, Lester, Los Santos, Vinewood"
USE_CONTEXT_PROMPT = True        # az előző mondat(ok) segítik a felismerést
CONTEXT_RESET_S = 20.0           # ennyi csend után új jelenetnek veszi, törli a kontextust

# --- Beszédszakaszok (Silero VAD) ---
VAD_THRESHOLD = 0.5              # efölött beszéd
VAD_NEG_THRESHOLD = 0.35         # ez alatt csend (a kettő között marad az előző állapot)
START_SPEECH_MS = 160            # ennyi folyamatos beszéd indít egy szakaszt
MIN_UTTERANCE_SPEECH_MS = 250    # ennél kevesebb beszéd zaj (lövés, ajtócsapás): eldobjuk
END_SILENCE_MS = 450             # ennyi csend után lezárjuk a mondatot
SPECULATIVE_SILENCE_MS = 250     # ekkor már előre felismer és fordít (0 = kikapcsolva)
SMART_ENDPOINT = True            # ha ekkor a szöveg mondatvégi írásjellel zárul, rögtön lezárja
PRE_ROLL_MS = 300                # a beszéd eleje elől ennyi hangot is hozzáveszünk
POST_ROLL_MS = 200               # a beszéd vége után ennyi csend marad a darabban
MAX_UTTERANCE_S = 12.0           # biztosíték: ennél hosszabb szakaszt a legcsendesebb ponton vágjuk

# --- Élő előnézet és korai lezárás ---
PARTIAL_INTERVAL_S = 0.5         # milyen gyakran frissüljön az angol előnézet (0 = ki)
EARLY_COMMIT = True              # hosszú beszédnél a kész mondatokat rögtön kiküldi
EARLY_COMMIT_MIN_S = 2.5         # csak ennél hosszabb szakasznál próbálkozik
EARLY_COMMIT_GUARD_S = 0.6       # a mondatvég után legalább ennyi hang kell már

# --- Fordítás ---
TRANSLATOR = "deepl"             # "deepl" | "ollama" | "google" | "google_web" | "none"
                                 # deepl: ha nem válaszol, helyi Ollama (ha fut), különben angol szöveg
# A DeepL-kulcsot a "--deepl-key" (vagy az első indítás) menti ide, a felhasználói mappába, NEM a
# projektmappába, így nem kerül bele, ha továbbadod. (DEEPL_API_KEY környezeti változó is jó.)
DEEPL_KEY_FILE = os.path.join(os.path.expanduser("~"), ".rksubby", "deepl_key.txt")
DEEPL_FORMALITY = "prefer_less"  # tegező hangnem, ha a DeepL támogatja a nyelvnél (játékpárbeszédhez)
OLLAMA_URL = "http://localhost:11434"
OLLAMA_MODEL = "translategemma:12b"   # vagy pl. "gemma3:12b" (az kontextust is kap)
CONTEXT_LINES = 3                # ennyi előző mondat megy kontextusnak (DeepL / LLM)
TRANSLATE_TIMEOUT_S = 6.0

# --- Megjelenítés ---
OBS_HTTP_PORT = 8765             # OBS böngészőforrás URL-je: http://127.0.0.1:8765/  (0 = ki)
WRITE_SUB_FILES = True           # sub.txt + sub.json is (a régi overlay-hez / helyi fájlos sub.html-hez)
SUB_TXT_PATH = os.path.join(BASE_DIR, "sub.txt")
SUB_JSON_PATH = os.path.join(BASE_DIR, "sub.json")
OVERLAY = False                  # beépített overlay-ablak (--overlay kapcsolja be)
OVERLAY_SCREEN = 0               # melyik monitoron (--overlay indításkor kiírja a listát; --screen N)
FONT_FAMILY = "Segoe UI"
FONT_SIZE = 34                   # pixel
SHOW_PARTIAL_EN = False          # beszéd közben szürke angol előnézet (True = bekapcsolva)
SHOW_EN_UNDER_HU = False         # a magyar alatt kicsiben az angol eredeti is
MAX_LINES = 2                    # egyszerre ennyi mondat látszik
SUB_MIN_S = 1.8                  # egy felirat legalább ennyi ideig látszik
SUB_MAX_S = 7.0
READ_CHARS_PER_S = 14            # olvasási tempó a kijelzési időhöz
BOTTOM_MARGIN = 0.07             # a képernyő aljától (a magasság arányában)
STATUS = True                    # felirat nélküli időszakban időnként kiírja a hangszintet
LOG_PATH = os.path.join(BASE_DIR, "sub_log.tsv")   # EN / HU / késés mondatonként ("" = ki)

# =============================================================================

def load_deepl_key():
    key = os.environ.get("DEEPL_API_KEY", "").strip()
    if key:
        return key
    try:
        with open(DEEPL_KEY_FILE, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


DEEPL_API_KEY = load_deepl_key()

FRAME = 512                      # a Silero VAD ablakmérete: 32 ms 16 kHz-en
FRAME_S = FRAME / SAMPLE_RATE
DEBUG = False


def ms2frames(ms):
    return max(1, int(round(ms / 1000.0 / FRAME_S)))


def dbg(*a):
    if DEBUG:
        print("[debug]", *a, flush=True)


# -----------------------------------------------------------------------------
# Szövegsegédek
# -----------------------------------------------------------------------------

_NON_WORD = re.compile(r"[^\w']+", re.UNICODE)
_ABBREVIATIONS = {"mr", "mrs", "ms", "dr", "st", "jr", "sr", "vs", "lt", "sgt", "no"}


def norm_word(w):
    return _NON_WORD.sub("", w.lower())


def norm_text(t):
    return " ".join(x for x in (norm_word(w) for w in t.split()) if x)


def clean_spaces(t):
    t = re.sub(r"\s+", " ", t).strip()
    return re.sub(r"^[-–—]\s*", "", t)  # párbeszéd-gondolatjel az elején


def is_sentence_end(word):
    w = word.strip()
    if not w or w.endswith("...") or w.endswith("…"):
        return False  # "I..." – habozás, nem mondatvég
    if w[-1] not in ".?!":
        return False
    return not (w.endswith(".") and norm_word(w) in _ABBREVIATIONS)


def strip_overlap(prev_text, new_text, max_words=4, min_words=1):
    """Ha a vágás után az új darab az előző végét megismétli, levágja az átfedést.
    min_words=2: biztos mondathatáron vágtunk, ott egyetlen egyező szó valódi ismétlés is lehet."""
    if not prev_text or not new_text:
        return new_text
    pw = [norm_word(x) for x in prev_text.split()]
    raw = new_text.split()
    nw = [norm_word(x) for x in raw]
    for k in range(min(max_words, len(pw), len(nw)), max(1, min_words) - 1, -1):
        if pw[-k:] == nw[:k] and (k >= 2 or len(nw[0]) >= 4):
            return " ".join(raw[k:])   # akár üres is lehet: akkor az egész ismétlés volt
    return new_text


# A Whisper jellemző "kitalált" mondatai csendre/zajra
_SUSPICIOUS = {
    "thank you", "thanks", "thank you very much", "thank you so much", "you", "bye",
    "thanks for watching", "thank you for watching", "so", "oh", "uh", "um", "hmm", "i",
}


def looks_like_hallucination(r):
    t = norm_text(r.text)
    if not t:
        return True
    low = r.text.lower()
    if "amara.org" in low or "subtitles by" in low or "subscribe to" in low:
        return True
    if r.compression_ratio > 2.4:          # ismétlődő szemét
        return True
    if r.avg_logprob < -1.1:               # nagyon bizonytalan
        return True
    if t in _SUSPICIOUS and (r.no_speech_prob > 0.15 or r.avg_logprob < -0.55):
        return True
    return False


# -----------------------------------------------------------------------------
# Beszédfelismerés
# -----------------------------------------------------------------------------

@dataclass
class AsrWord:
    start: float
    end: float
    word: str


@dataclass
class AsrResult:
    text: str
    words: list = field(default_factory=list)
    avg_logprob: float = 0.0
    no_speech_prob: float = 0.0
    compression_ratio: float = 1.0
    ms: float = 0.0


def cuda_available():
    try:
        import ctranslate2

        return ctranslate2.get_cuda_device_count() > 0
    except Exception:
        return False


class WhisperASR:
    def __init__(self, model_name, device, compute_type):
        from faster_whisper import WhisperModel

        try:
            # Először csak a helyi gyorsítótárból: nem kérdezi le minden indításkor a Hugging Face-t
            self.model = WhisperModel(model_name, device=device, compute_type=compute_type, local_files_only=True)
        except Exception as e:
            if not any(c.__name__ in ("LocalEntryNotFoundError", "FileNotFoundError") for c in type(e).__mro__):
                raise
            print("      A modell még nincs letöltve: letöltöm (egyszeri, kb. 1,5 GB, pár perc)...", flush=True)
            self.model = WhisperModel(model_name, device=device, compute_type=compute_type)

    def transcribe(self, audio, beam_size=1, word_timestamps=False, prompt=None):
        t0 = time.perf_counter()
        segments, _info = self.model.transcribe(
            audio,
            language=LANGUAGE,
            task="transcribe",
            beam_size=beam_size,
            temperature=0.0,                    # nincs lassú "újrapróbálás" magasabb hőmérséklettel
            condition_on_previous_text=False,
            initial_prompt=prompt or None,
            hotwords=GAME_HOTWORDS or None,
            without_timestamps=not word_timestamps,
            word_timestamps=word_timestamps,
            vad_filter=False,                   # a VAD-ot mi csináljuk, folyamatosan
        )
        segs = list(segments)                   # a generátor itt fut le ténylegesen
        words = []
        for s in segs:
            for w in s.words or []:
                words.append(AsrWord(w.start, w.end, w.word))
        res = AsrResult(text=clean_spaces("".join(s.text for s in segs)), words=words)
        if segs:
            res.avg_logprob = float(np.mean([s.avg_logprob for s in segs]))
            res.no_speech_prob = float(max(s.no_speech_prob for s in segs))
            res.compression_ratio = float(max(s.compression_ratio for s in segs))
        res.ms = (time.perf_counter() - t0) * 1000
        return res


class StreamingVAD:
    """Silero VAD a faster-whisper saját ONNX modelljével, 32 ms-onként, állapottal."""

    def __init__(self):
        import faster_whisper
        import onnxruntime

        path = os.path.join(os.path.dirname(faster_whisper.__file__), "assets", "silero_vad_v6.onnx")
        if not os.path.exists(path):
            raise SystemExit(
                "Nem találom a faster-whisper VAD modelljét (silero_vad_v6.onnx).\n"
                "Frissíts: pip install -U faster-whisper   (legalább 1.2 kell)"
            )
        opts = onnxruntime.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        opts.log_severity_level = 4
        self.sess = onnxruntime.InferenceSession(path, providers=["CPUExecutionProvider"], sess_options=opts)
        self.h = np.zeros((1, 1, 128), np.float32)
        self.c = np.zeros((1, 1, 128), np.float32)
        self.ctx = np.zeros((1, 64), np.float32)

    def __call__(self, frame):
        x = np.concatenate([self.ctx, frame[None, :]], axis=1)
        out, self.h, self.c = self.sess.run(None, {"input": x, "h": self.h, "c": self.c})
        self.ctx = x[:, -64:]
        return float(out.reshape(-1)[0])


class Utterance:
    """Egy folyamatban lévő beszédszakasz: 32 ms-os ablakok + VAD-valószínűségek."""

    def __init__(self, continuation=False):
        self.frames, self.probs, self.times = [], [], []
        self.last_speech = -1        # az utolsó beszédnek ítélt ablak indexe
        self.silence_run = 0         # azóta eltelt csendes ablakok száma
        self.speech_frames = 0
        self.continuation = continuation  # egy korábbi vágás folytatása-e

    def append(self, frame, p, t):
        self.frames.append(frame)
        self.probs.append(p)
        self.times.append(t)
        i = len(self.frames) - 1
        if p >= VAD_THRESHOLD:
            self.speech_frames += 1
            self.silence_run = 0
            self.last_speech = i
        elif p < VAD_NEG_THRESHOLD or self.silence_run > 0:
            self.silence_run += 1
        else:
            self.last_speech = i     # a két küszöb között: folytatódik a beszéd

    def __len__(self):
        return len(self.frames)

    def duration(self):
        return len(self.frames) * FRAME_S

    def audio(self, end=None):
        fr = self.frames[:end]
        return np.concatenate(fr) if fr else np.zeros(0, np.float32)

    def tail_from(self, idx):
        u = Utterance(continuation=True)
        for f, p, t in zip(self.frames[idx:], self.probs[idx:], self.times[idx:]):
            u.append(f, p, t)
        return u


@dataclass
class FinalItem:
    en: str
    future: object
    t_speech_end: float
    asr_ms: float


class LiveSubtitler:
    """VAD-alapú darabolás, élő előnézet, korai lezárás, spekulatív felismerés+fordítás."""

    def __init__(self, asr, vad, translator, display, display_q):
        self.asr, self.vad, self.translator = asr, vad, translator
        self.display, self.display_q = display, display_q
        self.start_frames = ms2frames(START_SPEECH_MS)
        self.min_utt_frames = ms2frames(MIN_UTTERANCE_SPEECH_MS)
        self.end_frames = ms2frames(END_SILENCE_MS)
        self.spec_frames = ms2frames(SPECULATIVE_SILENCE_MS) if 0 < SPECULATIVE_SILENCE_MS < END_SILENCE_MS else 0
        self.post_frames = ms2frames(POST_ROLL_MS)
        self.preroll = collections.deque(maxlen=ms2frames(PRE_ROLL_MS) + self.start_frames)
        self.start_run = 0
        self.utt = None
        self.prefinal = None         # (last_speech index, szöveg)
        self.prev_words = []         # az előző előnézet szavai (egyezésvizsgálathoz)
        self.last_partial_t = 0.0
        self.context = collections.deque(maxlen=max(1, CONTEXT_LINES))
        self.last_final_t = 0.0
        self.last_committed = ""
        self.cut_kind = None
        self.lag = 0.0
        # állapotkijelzés (hangszint), hogy látszódjon, jön-e hang
        self.total_frames = 0
        self.next_status = ms2frames(5000)
        self.st_frames, self.st_sumsq, self.st_peak, self.st_vad, self.st_finals = 0, 0.0, 0.0, 0.0, 0

    # ---------------------------------------------------------------- fő ciklus
    def run(self, audio_q, stop):
        buf = np.zeros(0, np.float32)
        marks = collections.deque()  # (a darab vége a bufferben, érkezési idő)
        while not stop.is_set():
            try:
                item = audio_q.get(timeout=0.1)
            except queue.Empty:
                item = None
            while item is not None:
                chunk, t_arr = item
                buf = np.concatenate([buf, chunk])
                marks.append((len(buf), t_arr))
                try:
                    item = audio_q.get_nowait()
                except queue.Empty:
                    item = None
            pos = 0
            while pos + FRAME <= len(buf):
                end = pos + FRAME
                while len(marks) > 1 and marks[0][0] < end:
                    marks.popleft()
                t = marks[0][1] if marks else time.monotonic()
                try:
                    self._on_frame(buf[pos:end], t)
                except Exception as e:  # egy hibás darab ne állítsa le a feliratozót
                    print(f"[hiba a feldolgozásban] {e!r}", flush=True)
                    self._end_utterance()
                pos = end
            if pos:
                buf = buf[pos:]
                marks = collections.deque((e - pos, t) for e, t in marks)
            try:
                self._maybe_partial()
            except Exception as e:
                print(f"[hiba az előnézetben] {e!r}", flush=True)

    # ------------------------------------------------------------- egy ablak
    def _on_frame(self, frame, t):
        p = self.vad(frame)
        self.lag = time.monotonic() - t
        self._stats(frame, p)
        if self.utt is None:
            self.preroll.append((frame, p, t))
            self.start_run = self.start_run + 1 if p >= VAD_THRESHOLD else 0
            if self.start_run >= self.start_frames:
                self._start_utterance()
            return
        u = self.utt
        u.append(frame, p, t)
        if self.spec_frames and u.silence_run == self.spec_frames:
            self._prefinal()
            pf = self.prefinal
            if SMART_ENDPOINT and pf and pf[1] and is_sentence_end(pf[1].split()[-1]):
                self._finalize_silence()   # mondatvégi írásjel + rövid szünet: nem várunk tovább
                return
        if u.silence_run >= self.end_frames:
            self._finalize_silence()
        elif u.duration() >= MAX_UTTERANCE_S:
            self._finalize_max()

    def _stats(self, frame, p):
        self.total_frames += 1
        self.st_frames += 1
        self.st_sumsq += float(np.dot(frame, frame))
        self.st_peak = max(self.st_peak, float(np.max(np.abs(frame))))
        self.st_vad = max(self.st_vad, p)
        if self.total_frames >= self.next_status:
            self._status()

    def _status(self):
        """Ha egy ideje nem volt felirat: kiírja, jön-e hang és hall-e benne beszédet."""
        elapsed = self.total_frames * FRAME_S
        self.next_status = self.total_frames + ms2frames((10 if elapsed < 60 else 60) * 1000)
        rms_db = 10 * np.log10(max(self.st_sumsq / max(1, self.st_frames * FRAME), 1e-12))
        peak_db = 20 * np.log10(max(self.st_peak, 1e-6))
        if STATUS and self.st_finals == 0:
            if peak_db < -60:
                msg = ("NEM JÖN HANG (néma a bemenet). Szól a játék/videó? Az OBS-ben a játék hangja "
                       "tényleg a CABLE Inputra megy?")
            elif self.st_vad < VAD_THRESHOLD:
                msg = f"jön hang ({rms_db:.0f} dBFS, csúcs {peak_db:.0f} dBFS), de beszédet nem hallok benne"
            else:
                msg = f"hallok beszédet ({rms_db:.0f} dBFS), feldolgozom"
            lag = f", lemaradás: {self.lag:.1f} s" if self.lag > 1.0 else ""
            print(f"[állapot {elapsed:5.0f} s] {msg}{lag}", flush=True)
        self.st_frames, self.st_sumsq, self.st_peak, self.st_vad, self.st_finals = 0, 0.0, 0.0, 0.0, 0

    def _start_utterance(self):
        if self.last_final_t and time.monotonic() - self.last_final_t > CONTEXT_RESET_S:
            self.context.clear()
            self.last_committed = ""
        self.utt = Utterance()
        for f, p, t in self.preroll:
            self.utt.append(f, p, t)
        self.preroll.clear()
        self.start_run = 0
        self.prefinal = None
        self.prev_words = []
        self.last_partial_t = time.monotonic()
        dbg("beszéd kezdete")

    def _end_utterance(self):
        self.utt = None
        self.prefinal = None
        self.prev_words = []
        self.display.partial("")

    # ------------------------------------------------------------- felismerés
    def _prompt(self):
        if not USE_CONTEXT_PROMPT or not self.context:
            return None
        return " ".join(self.context)[-220:]

    def _clean(self, res):
        if looks_like_hallucination(res):
            if res.text:
                dbg(f"kiszűrve: {res.text!r} (logprob {res.avg_logprob:.2f}, no_speech {res.no_speech_prob:.2f})")
            return ""
        return res.text

    def _strip(self, text):
        return strip_overlap(self.last_committed, text, min_words=2 if self.cut_kind == "sentence" else 1)

    def _speech_audio(self, u):
        return u.audio(min(len(u), u.last_speech + 1 + self.post_frames))

    def _prefinal(self):
        """A csend elején: végleges minőségű felismerés + a fordítás előre elindítva."""
        u = self.utt
        if u.speech_frames < self.min_utt_frames:
            return
        res = self.asr.transcribe(self._speech_audio(u), beam_size=FINAL_BEAM_SIZE, prompt=self._prompt())
        text = self._clean(res)
        if u.continuation:
            text = self._strip(text)
        self.prefinal = (u.last_speech, text, res.ms)
        if text:
            self.display.partial(text)
            self.translator.submit(text, list(self.context))
        dbg(f"előzetes ({res.ms:.0f} ms): {text!r}")

    def _finalize_silence(self):
        u = self.utt
        dur = (u.last_speech + 1) * FRAME_S
        if u.speech_frames < self.min_utt_frames:
            dbg(f"eldobva: túl rövid ({u.speech_frames * FRAME_S * 1000:.0f} ms beszéd)")
            self._end_utterance()
            return
        if self.prefinal and self.prefinal[0] == u.last_speech:
            text, asr_ms = self.prefinal[1], self.prefinal[2]   # az előzetes eredmény még érvényes
        else:
            res = self.asr.transcribe(self._speech_audio(u), beam_size=FINAL_BEAM_SIZE, prompt=self._prompt())
            text, asr_ms = self._clean(res), res.ms
            if u.continuation:
                text = self._strip(text)
        dbg(f"szakasz vége (csend), {dur:.2f} s")
        self._emit_final(text, u.times[u.last_speech], asr_ms)
        self._end_utterance()

    def _finalize_max(self):
        u = self.utt
        n = len(u)
        lo, hi = max(1, n - ms2frames(3000)), max(2, n - ms2frames(200))
        cut = lo + int(np.argmin(u.probs[lo:hi]))
        res = self.asr.transcribe(u.audio(cut + 1), beam_size=FINAL_BEAM_SIZE, prompt=self._prompt())
        text = self._clean(res)
        if u.continuation:
            text = self._strip(text)
        dbg(f"szakasz vágása (max hossz) {(cut + 1) * FRAME_S:.2f} s-nál")
        self._emit_final(text, u.times[cut], res.ms)
        self.utt = u.tail_from(cut + 1)
        self.cut_kind = "max"          # kényszervágás: egy szó átlóghat, az ismétlést ki kell szűrni
        self.prefinal = None
        self.prev_words = []

    def _maybe_partial(self):
        u = self.utt
        if u is None or PARTIAL_INTERVAL_S <= 0 or u.silence_run > 0:
            return
        now = time.monotonic()
        if now - self.last_partial_t < PARTIAL_INTERVAL_S or u.speech_frames * FRAME_S < 0.5:
            return
        if self.lag > 0.6:           # le vagyunk maradva: előbb behozzuk a hangot
            return
        self.last_partial_t = now
        want_words = EARLY_COMMIT and u.duration() >= EARLY_COMMIT_MIN_S
        res = self.asr.transcribe(u.audio(), beam_size=PARTIAL_BEAM_SIZE, word_timestamps=want_words,
                                  prompt=self._prompt())
        if want_words and res.words and not looks_like_hallucination(res):
            commit = self._find_commit(res.words)
            if commit:
                k, cut = commit
                head = clean_spaces("".join(w.word for w in res.words[:k]))
                rest = clean_spaces("".join(w.word for w in res.words[k:]))
                if u.continuation:
                    head = self._strip(head)
                dbg(f"korai lezárás {(cut + 1) * FRAME_S:.2f} s-nál: {head!r}")
                self._emit_final(head, u.times[cut], res.ms)
                self.utt = u.tail_from(cut + 1)
                self.cut_kind = "sentence"   # biztos mondathatár a két szó közötti csendben
                self.prefinal = None
                self.prev_words = [norm_word(w.word) for w in res.words[k:]]
                self.display.partial(rest)
                return
            cand = self._candidate_end(res.words)
            if cand is not None:   # a mondatvég már látszik: a fordítás előre indul, mire lezárjuk, kész lesz
                head = clean_spaces("".join(w.word for w in res.words[:cand + 1]))
                if u.continuation:
                    head = self._strip(head)
                if norm_text(head):
                    self.translator.submit(head, list(self.context))
            self.prev_words = [norm_word(w.word) for w in res.words]
        else:
            self.prev_words = []
        text = self._clean(res)
        if text and u.continuation:
            text = self._strip(text)
        self.display.partial(text)

    def _candidate_end(self, words):
        """Az utolsó mondatvég, amely után már elég hang van (EARLY_COMMIT_GUARD_S)."""
        dur = self.utt.duration()
        cand = None
        for i in range(len(words) - 1):
            if is_sentence_end(words[i].word) and words[i].end <= dur - EARLY_COMMIT_GUARD_S:
                cand = i
        return cand

    def _find_commit(self, words):
        """Az utolsó olyan mondatvég, amelyet két egymás utáni felismerés is ugyanígy hallott."""
        u = self.utt
        cand = self._candidate_end(words)
        if cand is None:
            return None
        k = cand + 1
        cur = [norm_word(w.word) for w in words[:k]]
        if self.prev_words[:k] != cur:
            return None
        a = words[cand].end
        b = max(a, words[cand + 1].start)
        lo = max(1, int((a - 0.08) / FRAME_S))
        hi = min(len(u) - 1, int((b + 0.08) / FRAME_S) + 1)
        if hi <= lo:
            cut = min(max(1, int(a / FRAME_S)), len(u) - 2)
        else:
            cut = lo + int(np.argmin(u.probs[lo:hi]))   # a két szó közti legcsendesebb pont
        return k, cut

    def _emit_final(self, text, t_speech_end, asr_ms):
        text = clean_spaces(text or "")
        if not norm_text(text):
            return
        fut = self.translator.submit(text, list(self.context))
        self.context.append(text)
        self.last_committed = text
        self.last_final_t = time.monotonic()
        self.st_finals += 1
        self.display_q.put(FinalItem(text, fut, t_speech_end, asr_ms))


# -----------------------------------------------------------------------------
# Fordítás
# -----------------------------------------------------------------------------

_TG_PROMPT = (
    "You are a professional English (en) to Hungarian (hu) translator. Your goal is to accurately "
    "convey the meaning and nuances of the original English text while adhering to Hungarian grammar, "
    "vocabulary, and cultural sensitivities.\n"
    "Produce only the Hungarian translation, without any additional explanations or commentary. "
    "Please translate the following English text into Hungarian:\n\n\n{text}"
)

_LLM_SYSTEM = (
    "You translate English video game dialogue (Grand Theft Auto) into natural, colloquial Hungarian "
    "subtitles. Keep the meaning, tone and slang; swearing is fine. Keep names unchanged. "
    "Use the previous lines only as context. Output ONLY the Hungarian translation of the line to "
    "translate, with no quotes or explanations."
)


class _ThreadLocalSession:
    def __init__(self, headers=None):
        self.local = threading.local()
        self.headers = headers or {}

    def get(self):
        s = getattr(self.local, "s", None)
        if s is None:
            import requests

            s = requests.Session()           # keep-alive: nem kell minden kérésnél új TLS-kapcsolat
            s.headers.update(self.headers)
            self.local.s = s
        return s


def _short_err(e):
    s = str(e).strip()
    s = s.splitlines()[0] if s else type(e).__name__
    return s if len(s) <= 140 else s[:140] + "..."


def ollama_status():
    """(fut-e az Ollama, használható modell neve vagy None)"""
    try:
        import requests

        r = requests.get(OLLAMA_URL.rstrip("/") + "/api/tags", timeout=0.7)
        r.raise_for_status()
        names = [m.get("name", "") for m in r.json().get("models", [])]
    except Exception:
        return False, None
    want = OLLAMA_MODEL if ":" in OLLAMA_MODEL else OLLAMA_MODEL + ":latest"
    if want in names:
        return True, want
    for pref in ("translategemma:12b", "translategemma:27b", "translategemma:4b", "translategemma:latest"):
        if pref in names:
            return True, pref
    return True, None


def deepl_host(key):
    return "https://api-free.deepl.com" if key.endswith(":fx") else "https://api.deepl.com"


def _deepl_check(r):
    if r.status_code == 403:
        raise RuntimeError("a DeepL elutasította a kulcsot (403): rossz vagy letiltott kulcs")
    if r.status_code == 456:
        raise RuntimeError("elfogyott a havi DeepL-keret (456), a hónap elején újraindul")
    if r.status_code == 429:
        raise RuntimeError("túl sok kérés a DeepL felé (429)")
    r.raise_for_status()


def deepl_usage(key, timeout=8.0):
    """(felhasznált karakterek, havi keret) az aktuális időszakban."""
    import requests

    r = requests.get(deepl_host(key) + "/v2/usage", headers={"Authorization": f"DeepL-Auth-Key {key}"},
                     timeout=timeout)
    _deepl_check(r)
    d = r.json()
    return int(d.get("character_count", 0)), int(d.get("character_limit", 0))


def _fmt_int(n):
    return f"{n:,}".replace(",", " ")


_DEEPL_KEY_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}(?::fx)?")


def extract_deepl_key(text, strict=False):
    """Kiveszi a kulcsot a szövegből (akkor is, ha többször lett beillesztve, vagy szemét van körülötte)."""
    if not text:
        return ""
    found = _DEEPL_KEY_RE.findall(text)
    if found:
        return found[-1]
    if strict:
        return ""
    s = text.strip().strip('"').strip("'").strip()
    return s if re.fullmatch(r"[A-Za-z0-9:_-]{20,100}", s) else ""   # esetleges más formátumú kulcs


def mask_key(key):
    return key[:4] + "******" + key[-4:] if len(key) > 12 else "******"


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
    """A DeepL-kulcs beállítása: a vágólapról (Enterrel jóváhagyva), parancssori paraméterből vagy
    beillesztéssel. Ellenőrzi a DeepL-nél, és csak működő kulcsot ment el."""
    print("DeepL API-kulcs beállítása", flush=True)
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
            print("  A kulcs helye: https://www.deepl.com/your-account/keys (másolás ikon a kulcs mellett)")
            raw = input("  Illeszd be a kulcsot (jobb klikk vagy Ctrl+V), majd Enter: ")
            key = extract_deepl_key(raw)
            if not key:
                print("Nem találtam kulcsot a beillesztett szövegben. Másold ki újra a DeepL oldalán "
                      f"(másolás ikon), és futtasd: python {SCRIPT_NAME} --deepl-key")
                return 1
    except (EOFError, KeyboardInterrupt):
        print("\nMegszakítva.")
        return 1
    print(f"Kulcs: {mask_key(key)} ({'Free' if key.endswith(':fx') else 'Pro'}). Ellenőrzöm a DeepL-nél...",
          flush=True)
    try:
        used, limit = deepl_usage(key)
    except Exception as e:
        print(f"A kulcs nem működik: {_short_err(e)}")
        return 1
    os.makedirs(os.path.dirname(DEEPL_KEY_FILE), exist_ok=True)
    with open(DEEPL_KEY_FILE, "w", encoding="utf-8") as f:
        f.write(key)
    print(f"Rendben, a kulcs működik, elmentettem: {DEEPL_KEY_FILE}")
    print(f"Havi keret: {_fmt_int(used)} / {_fmt_int(limit)} karakter felhasználva.")
    if os.environ.get("DEEPL_API_KEY", "").strip() and os.environ["DEEPL_API_KEY"].strip() != key:
        print("Figyelem: a DEEPL_API_KEY környezeti változó is be van állítva, és az elsőbbséget élvez.")
    return 0


def make_backend(kind, ollama_model=None):
    kind = kind.lower()
    if kind == "none":
        return lambda text, ctx: text

    if kind == "google":
        # A Google ingyenes JSON-végpontja (ezt használják a böngészőbővítmények is).
        # A deep-translator által használt translate.google.com/m oldal 2026 nyara óta sokszor 429-et ad.
        sess = _ThreadLocalSession({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                                                  "Chrome/141.0 Safari/537.36"})

        def google(text, ctx):
            r = sess.get().get("https://translate.googleapis.com/translate_a/single",
                               params={"client": "gtx", "sl": "en", "tl": "hu", "dt": "t", "q": text},
                               timeout=TRANSLATE_TIMEOUT_S)
            if r.status_code == 429:
                raise RuntimeError("túl sok kérés (HTTP 429)")
            r.raise_for_status()
            data = r.json()
            return "".join(part[0] for part in data[0] if part and part[0])

        return google

    if kind == "google_web":
        from deep_translator import GoogleTranslator

        # Hívásonként új példány: a GoogleTranslator belső állapota nem szálbiztos
        return lambda text, ctx: GoogleTranslator(source="en", target="hu").translate(text)

    if kind == "deepl":
        if not DEEPL_API_KEY:
            raise RuntimeError(f"nincs DeepL-kulcs (python {SCRIPT_NAME} --deepl-key)")
        host = deepl_host(DEEPL_API_KEY)
        sess = _ThreadLocalSession({"Authorization": f"DeepL-Auth-Key {DEEPL_API_KEY}"})

        def deepl(text, ctx):
            body = {"text": [text], "source_lang": "EN", "target_lang": "HU"}
            if DEEPL_FORMALITY:
                body["formality"] = DEEPL_FORMALITY
            if ctx:
                body["context"] = " ".join(ctx)   # nem fordítja le és nem számolja bele a keretbe
            r = sess.get().post(host + "/v2/translate", json=body, timeout=TRANSLATE_TIMEOUT_S)
            _deepl_check(r)
            return r.json()["translations"][0]["text"]

        return deepl

    if kind == "ollama":
        model = ollama_model or OLLAMA_MODEL
        sess = _ThreadLocalSession()
        url = OLLAMA_URL.rstrip("/")
        is_tg = "translategemma" in model.lower()
        timeout = max(TRANSLATE_TIMEOUT_S, 30.0)   # az első hívás betölti a modellt a GPU-ra

        def ollama(text, ctx):
            if is_tg:   # a TranslateGemma a saját, rögzített promptformátumát várja
                body = {"model": model, "prompt": _TG_PROMPT.format(text=text), "stream": False,
                        "keep_alive": "30m", "options": {"temperature": 0}}
                r = sess.get().post(url + "/api/generate", json=body, timeout=timeout)
                r.raise_for_status()
                return r.json()["response"].strip()
            user = ""
            if ctx:
                user += "Previous lines (context only):\n" + "\n".join(ctx) + "\n\n"
            user += "Line to translate:\n" + text
            body = {"model": model, "stream": False, "keep_alive": "30m",
                    "options": {"temperature": 0.2},
                    "messages": [{"role": "system", "content": _LLM_SYSTEM},
                                 {"role": "user", "content": user}]}
            r = sess.get().post(url + "/api/chat", json=body, timeout=timeout)
            r.raise_for_status()
            return r.json()["message"]["content"].strip().strip('"')

        return ollama

    raise RuntimeError(f"ismeretlen fordító: {kind}")


AUTO_CHAIN = ["deepl", "ollama"]   # tartalék sorrend; a Google csak kifejezett kérésre (--translator google)


def build_chain(kind, verbose=True):
    """A választott fordító elöl, mögötte tartaléknak a többi elérhető (ha az első nem válaszol)."""
    kind = kind.lower()
    if kind == "none":
        return []
    order = AUTO_CHAIN if kind == "auto" else [kind] + [k for k in AUTO_CHAIN if k != kind]
    chain = []
    for k in order:
        try:
            if k == "deepl" and not DEEPL_API_KEY:
                if kind in ("deepl", "auto") and verbose:
                    print(f"[fordító] NINCS DeepL-kulcs. Beállítás: python {SCRIPT_NAME} --deepl-key", flush=True)
                continue
            if k == "ollama":
                running, model = ollama_status()
                if not model:
                    if kind == "ollama" and verbose:
                        print("[fordító] Ollama: " + ("fut, de nincs letöltve fordítómodell: ollama pull "
                                                      "translategemma:12b" if running else "nem fut."), flush=True)
                    continue
                chain.append((f"ollama ({model})", make_backend("ollama", model)))
                continue
            chain.append((k, make_backend(k)))
        except Exception as e:
            if verbose:
                print(f"[fordító] {k} nem használható: {_short_err(e)}", flush=True)
    return chain


def test_translators():
    """--test-translate: minden fordítót kipróbál egy mondaton (Whisper nélkül, pár másodperc)."""
    sample = "Get in the car, Franklin, before the cops get here!"
    print(f"Fordítók tesztje: \"{sample}\"\n", flush=True)
    for k in ("deepl", "ollama", "google", "google_web"):
        name = k
        try:
            if k == "deepl" and not DEEPL_API_KEY:
                print(f"  deepl       : nincs kulcs -> python {SCRIPT_NAME} --deepl-key", flush=True)
                continue
            if k == "deepl":
                used, limit = deepl_usage(DEEPL_API_KEY)
                print(f"  deepl       : havi keret {_fmt_int(used)} / {_fmt_int(limit)} karakter", flush=True)
            if k == "ollama":
                running, model = ollama_status()
                if not model:
                    print("  ollama      : " + ("fut, de nincs fordítómodell -> ollama pull translategemma:12b"
                                                if running else "nem fut (vagy nincs telepítve)"), flush=True)
                    continue
                fn, name = make_backend("ollama", model), f"ollama ({model})"
            else:
                fn = make_backend(k)
            t0 = time.perf_counter()
            out = fn(sample, [])
            print(f"  {name:<12}: OK   {(time.perf_counter() - t0) * 1000:5.0f} ms  -> {out}", flush=True)
        except Exception as e:
            print(f"  {name:<12}: HIBA -> {_short_err(e)}", flush=True)


class Translator:
    """Aszinkron fordítás gyorsítótárral (ugyanazt a mondatot csak egyszer fordítja le) és
    tartalékkal: ha egy szolgáltatás hibázik, egy ideig a következőt használja."""

    def __init__(self, kind):
        self.chain = build_chain(kind)
        self.down_until = {name: 0.0 for name, _ in self.chain}
        self.backoff = {name: 30.0 for name, _ in self.chain}
        self.last_used = None
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="fordito")
        self.cache = collections.OrderedDict()
        self.lock = threading.Lock()

    def names(self):
        return " -> ".join(name for name, _ in self.chain) or "nincs (angol szöveg jelenik meg)"

    def probe(self):
        """Indításkori próba: kiderül, melyik fordító működik, és felépül a kapcsolat."""
        if not self.chain:
            return "kikapcsolva, az angol szöveg jelenik meg"
        try:
            hu, ms = self.submit("Get in the car.", []).result(timeout=90)
        except Exception:
            hu, ms = None, 0
        if hu:
            return f"{self.last_used} működik ('Get in the car.' -> '{hu}', {ms:.0f} ms)"
        return f"EGYIK FORDÍTÓ SEM VÁLASZOLT, az angol szöveg jelenik meg (python {SCRIPT_NAME} --test-translate)"

    def submit(self, text, context):
        key = text.strip()
        with self.lock:
            fut = self.cache.get(key)
            if fut is not None:
                self.cache.move_to_end(key)
                return fut
            fut = self.pool.submit(self._run, key, tuple(context))
            self.cache[key] = fut
            while len(self.cache) > 200:
                self.cache.popitem(last=False)
            return fut

    def _run(self, text, context):
        t0 = time.perf_counter()
        for name, fn in self.chain:
            if time.monotonic() < self.down_until[name]:
                continue
            try:
                out = fn(text, list(context))
                if not out or not out.strip():
                    raise RuntimeError("üres válasz")
                with self.lock:
                    self.backoff[name] = 30.0
                self.last_used = name
                return clean_spaces(out), (time.perf_counter() - t0) * 1000
            except Exception as e:
                with self.lock:
                    wait = self.backoff[name]
                    self.down_until[name] = time.monotonic() + wait
                    self.backoff[name] = min(300.0, wait * 2)
                rest = "a tartalék fordító megy" if len(self.chain) > 1 else "angol felirat jelenik meg"
                print(f"[fordító] {name}: {_short_err(e)} -> {wait:.0f} mp szünet, addig {rest}", flush=True)
        with self.lock:
            self.cache.pop(text, None)   # legközelebb újrapróbálja
        return None, (time.perf_counter() - t0) * 1000


# -----------------------------------------------------------------------------
# Megjelenítés: konzol, napló, sub.txt / sub.json (OBS), overlay
# -----------------------------------------------------------------------------

def display_seconds(text):
    return min(SUB_MAX_S, max(SUB_MIN_S, len(text) / READ_CHARS_PER_S))


def atomic_write(path, text):
    """Az olvasó (OBS) soha nem lát félig írt fájlt; ha a csere nem megy, közvetlenül ír."""
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        for _ in range(10):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:     # Windows: épp olvassa a másik program
                time.sleep(0.01)
    except OSError:
        pass
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
    except OSError as e:
        dbg(f"nem tudom írni: {path}: {e}")


class SubtitleSink:
    """A látható feliratok állapota. Kiírja a konzolra és a naplóba, frissíti a sub.txt / sub.json
    fájlt (az OBS-es sub.html ezekből olvas), és továbbítja az overlay-nek, ha az fut."""

    def __init__(self, write_files, log_path):
        self.write_files = write_files
        self.on_final = None     # overlay-callback(hu, en)
        self.on_partial = None   # overlay-callback(en)
        self.lock = threading.Lock()
        self.lines = []          # [magyar, angol, lejárat]
        self.partial_text, self.partial_until = "", 0.0
        self.last_written = None
        self.log = None
        if log_path:
            try:
                new = not os.path.exists(log_path)
                self.log = open(log_path, "a", encoding="utf-8", buffering=1)
                if new:
                    self.log.write("ido\tangol\tmagyar\tasr_ms\tforditas_ms\tkeses_ms\n")
            except OSError as e:
                print(f"[figyelem] A naplót nem tudom írni ({log_path}): {e}", flush=True)
        with self.lock:
            self._write()
        threading.Thread(target=self._ticker, daemon=True, name="felirat-ido").start()

    def partial(self, en):
        if not SHOW_PARTIAL_EN:
            return
        if self.on_partial:
            self.on_partial(en)
        with self.lock:
            self.partial_text = en or ""
            self.partial_until = time.monotonic() + 3.0
            self._write()

    def final(self, hu, en, asr_ms, tr_ms, latency_ms):
        print(f"EN: {en}\nHU: {hu}\n    (ASR {asr_ms:.0f} ms, fordítás {tr_ms:.0f} ms, "
              f"késés a mondat végétől: {latency_ms:.0f} ms)\n", flush=True)
        if self.on_final:
            self.on_final(hu, en)
        if self.log:
            row = [time.strftime("%H:%M:%S"), en, hu, f"{asr_ms:.0f}", f"{tr_ms:.0f}", f"{latency_ms:.0f}"]
            self.log.write("\t".join(x.replace("\t", " ") for x in row) + "\n")
        with self.lock:
            self.lines.append([hu, en, time.monotonic() + display_seconds(hu)])
            del self.lines[:-MAX_LINES]
            self._write()

    def clear(self):
        with self.lock:
            self.lines, self.partial_text = [], ""
            self._write()

    def state_json(self):
        """Az aktuális feliratállapot JSON-ban (a beépített webszerver ezt adja az OBS-nek)."""
        with self.lock:
            return self._state()[1]

    def _ticker(self):
        while True:
            time.sleep(0.1)
            now = time.monotonic()
            with self.lock:
                changed = False
                if any(line[2] < now for line in self.lines):
                    self.lines = [line for line in self.lines if line[2] >= now]
                    changed = True
                if self.partial_text and now > self.partial_until:
                    self.partial_text = ""
                    changed = True
                if changed:
                    self._write()

    def _state(self):
        txt = "\n".join(line[0] for line in self.lines)
        state = {"lines": [{"hu": line[0], "en": line[1] if SHOW_EN_UNDER_HU else ""} for line in self.lines],
                 "partial": self.partial_text}
        return txt, json.dumps(state, ensure_ascii=False)

    def _write(self):
        """A lock alatt hívandó. Csak akkor ír fájlt, ha változott a tartalom."""
        if not self.write_files:
            return
        txt, js = self._state()
        if (txt, js) == self.last_written:
            return
        self.last_written = (txt, js)
        atomic_write(SUB_TXT_PATH, txt)
        atomic_write(SUB_JSON_PATH, js)


OVERLAY_HTML = r"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  html, body { background-color: rgba(0,0,0,0); margin: 0px auto; overflow: hidden; }
  #sub { position: absolute; bottom: 20px; width: 100%; text-align: center; font-family: Arial, sans-serif; }
  .hu, .en, .partial {
    padding: 0 40px;
    text-shadow: 2px 2px 4px #000, -2px -2px 4px #000, 2px -2px 4px #000, -2px 2px 4px #000;
  }
  .hu { color: yellow; font-size: 32px; font-weight: bold; line-height: 1.25; margin-top: 6px; }
  .en { color: #dddddd; font-size: 20px; font-style: italic; }
  .partial { color: #bbbbbb; font-size: 22px; font-style: italic; margin-top: 8px; }
</style>
</head>
<body>
  <div id="sub"></div>
  <script>
    var box = document.getElementById('sub');
    var last = null;
    function add(cls, text) {
      var d = document.createElement('div');
      d.className = cls;
      d.textContent = text;
      box.appendChild(d);
    }
    function render(state) {
      var key = JSON.stringify(state);
      if (key === last) return;
      last = key;
      box.textContent = '';
      (state.lines || []).forEach(function (line) {
        add('hu', line.hu);
        if (line.en) add('en', line.en);
      });
      if (state.partial) add('partial', state.partial);
    }
    function tick() {
      fetch('/state?t=' + Date.now(), { cache: 'no-store' })
        .then(function (r) { return r.json(); })
        .then(render)
        .catch(function () { render({ lines: [] }); })
        .then(function () { setTimeout(tick, 100); });
    }
    tick();
  </script>
</body>
</html>
"""


def start_overlay_server(sink, port):
    """Helyi webszerver az OBS böngészőforrásnak: http://127.0.0.1:<port>/ (fájlútvonal nélkül)."""
    import http.server

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html", "/sub.html"):
                body, ctype = OVERLAY_HTML.encode("utf-8"), "text/html; charset=utf-8"
            elif path in ("/state", "/sub.json"):
                body, ctype = sink.state_json().encode("utf-8"), "application/json; charset=utf-8"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    try:
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError as e:
        print(f"[figyelem] Az OBS-felirat webszervere nem indult (port {port}: {e}). "
              f"Fut még egy példány a scriptből?", flush=True)
        return None
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True, name="obs-felirat").start()
    return srv


def display_loop(display_q, display, stop):
    while not stop.is_set():
        try:
            item = display_q.get(timeout=0.2)
        except queue.Empty:
            continue
        try:
            hu, tr_ms = item.future.result(timeout=TRANSLATE_TIMEOUT_S + 2)
        except Exception:
            hu, tr_ms = None, -1.0
        latency = (time.monotonic() - item.t_speech_end) * 1000
        display.final(hu or item.en, item.en, item.asr_ms, tr_ms, latency)


def run_with_overlay(display, start_workers):
    """Beépített overlay. None-t ad vissza (és nem indít semmit), ha a PyQt5 nem érhető el."""
    try:
        from PyQt5 import QtCore, QtGui, QtWidgets
    except ImportError as e:
        print(f"[figyelem] Az overlay nem indítható ({e}). Telepítés: pip install PyQt5\n"
              f"           Megy tovább overlay nélkül (sub.txt / sub.json).", flush=True)
        return None

    Qt = QtCore.Qt
    QtWidgets.QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    app = QtWidgets.QApplication(sys.argv[:1])
    screens = QtWidgets.QApplication.screens()
    screen_idx = max(0, min(OVERLAY_SCREEN, len(screens) - 1))
    print("Overlay képernyők (--screen N):", flush=True)
    for i, s in enumerate(screens):
        g = s.geometry()
        desc = " ".join(x for x in (s.manufacturer(), s.model()) if x) or s.name()
        mark = "  <- ide kerül az overlay" if i == screen_idx else ""
        print(f"  {i}: {desc}  {g.width()}x{g.height()} @ ({g.x()},{g.y()}){mark}", flush=True)

    class Bridge(QtCore.QObject):
        final = QtCore.pyqtSignal(str, str)
        partial = QtCore.pyqtSignal(str)

    class Overlay(QtWidgets.QWidget):
        def __init__(self):
            super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
                             | Qt.WindowTransparentForInput)
            self.setAttribute(Qt.WA_TranslucentBackground)
            self.setAttribute(Qt.WA_ShowWithoutActivating)
            geo = screens[screen_idx].geometry()
            w, h = int(geo.width() * 0.9), int(geo.height() * 0.45)
            self.setGeometry(geo.x() + (geo.width() - w) // 2,
                             geo.y() + geo.height() - h - int(geo.height() * BOTTOM_MARGIN), w, h)
            self.font_hu = QtGui.QFont(FONT_FAMILY)
            self.font_hu.setPixelSize(FONT_SIZE)
            self.font_hu.setWeight(QtGui.QFont.DemiBold)
            self.font_small = QtGui.QFont(FONT_FAMILY)
            self.font_small.setPixelSize(int(FONT_SIZE * 0.68))
            self.font_small.setItalic(True)
            self.lines = collections.deque()   # [magyar, angol, lejárat]
            self.partial_text, self.partial_until = "", 0.0
            timer = QtCore.QTimer(self)
            timer.timeout.connect(self._tick)
            timer.start(150)
            self.show()

        def add_line(self, hu, en):
            self.lines.append([hu, en, time.monotonic() + display_seconds(hu)])
            while len(self.lines) > MAX_LINES:
                self.lines.popleft()
            self.update()

        def set_partial(self, en):
            self.partial_text = en
            self.partial_until = time.monotonic() + 3.0
            self.update()

        def _tick(self):
            now = time.monotonic()
            changed = False
            while self.lines and self.lines[0][2] < now:
                self.lines.popleft()
                changed = True
            if self.partial_text and now > self.partial_until:
                self.partial_text = ""
                changed = True
            if changed:
                self.update()

        def _wrap(self, text, fm, max_w):
            out, cur = [], ""
            for word in text.split():
                t = (cur + " " + word).strip()
                if not cur or fm.horizontalAdvance(t) <= max_w:
                    cur = t
                else:
                    out.append(cur)
                    cur = word
            if cur:
                out.append(cur)
            return out

        def paintEvent(self, _ev):
            blocks = []   # (sorok listája [(szöveg, font, szín)])
            for hu, en, _exp in self.lines:
                rows = [(r, self.font_hu, QtGui.QColor(255, 255, 255)) for r in
                        self._wrap(hu, QtGui.QFontMetrics(self.font_hu), self.width() - 60)]
                if SHOW_EN_UNDER_HU and en:
                    rows += [(r, self.font_small, QtGui.QColor(200, 200, 200)) for r in
                             self._wrap(en, QtGui.QFontMetrics(self.font_small), self.width() - 60)]
                blocks.append(rows)
            if SHOW_PARTIAL_EN and self.partial_text:
                blocks.append([(r, self.font_small, QtGui.QColor(190, 190, 190)) for r in
                               self._wrap(self.partial_text, QtGui.QFontMetrics(self.font_small),
                                          self.width() - 60)])
            if not blocks:
                return
            p = QtGui.QPainter(self)
            p.setRenderHint(QtGui.QPainter.Antialiasing)
            pad_x, pad_y, gap = 18, 8, 8
            heights = [sum(QtGui.QFontMetrics(f).height() for _, f, _ in rows) + 2 * pad_y for rows in blocks]
            while len(blocks) > 1 and sum(heights) + gap * (len(blocks) - 1) > self.height():
                blocks.pop(0)      # ha nem fér ki, a legrégebbi mondat tűnik el
                heights.pop(0)
            y = self.height() - sum(heights) - gap * (len(blocks) - 1)
            outline = QtGui.QPen(QtGui.QColor(0, 0, 0, 230), 4, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
            for rows, bh in zip(blocks, heights):
                bw = max(QtGui.QFontMetrics(f).horizontalAdvance(t) for t, f, _ in rows) + 2 * pad_x
                bx = (self.width() - bw) / 2
                p.setPen(Qt.NoPen)
                p.setBrush(QtGui.QColor(0, 0, 0, 150))
                p.drawRoundedRect(QtCore.QRectF(bx, y, bw, bh), 10, 10)
                ty = y + pad_y
                for text, font, color in rows:
                    fm = QtGui.QFontMetrics(font)
                    path = QtGui.QPainterPath()
                    path.addText((self.width() - fm.horizontalAdvance(text)) / 2, ty + fm.ascent(), font, text)
                    p.strokePath(path, outline)
                    p.fillPath(path, color)
                    ty += fm.height()
                y += bh + gap
            p.end()

    bridge = Bridge()
    overlay = Overlay()
    bridge.final.connect(overlay.add_line)
    bridge.partial.connect(overlay.set_partial)
    display.on_final = bridge.final.emit
    display.on_partial = bridge.partial.emit
    # Ctrl+C a konzolban: rendes kilépés (a Qt-ciklus mellett is lefut a Python jelkezelője)
    signal.signal(signal.SIGINT, lambda *_: app.quit())
    keepalive = QtCore.QTimer()
    keepalive.timeout.connect(lambda: None)
    keepalive.start(250)
    start_workers()
    app._overlay, app._keepalive = overlay, keepalive
    return app.exec_()


# -----------------------------------------------------------------------------
# Hangforrás: élő eszköz vagy WAV-lejátszás
# -----------------------------------------------------------------------------

class Resampler:
    """Egyszerű anti-alias szűrő + lineáris újramintavételezés (csak tartaléknak)."""

    def __init__(self, sr_in, sr_out):
        self.ratio = sr_in / sr_out
        n = 63
        fc = 0.45 * sr_out / sr_in
        k = np.arange(n) - (n - 1) / 2
        h = np.sinc(2 * fc * k) * np.hamming(n)
        self.h = (h / h.sum()).astype(np.float32)
        self.zi = np.zeros(n - 1, np.float32)
        self.prev = np.zeros(0, np.float32)
        self.pos = 0.0

    def process(self, x):
        xx = np.concatenate([self.zi, x.astype(np.float32)])
        y = np.convolve(xx, self.h, mode="valid")
        self.zi = xx[-(len(self.h) - 1):]
        buf = np.concatenate([self.prev, y])
        idx = np.arange(self.pos, len(buf) - 1, self.ratio)
        out = np.interp(idx, np.arange(len(buf)), buf).astype(np.float32)
        nxt = self.pos + len(idx) * self.ratio
        consumed = int(np.floor(nxt))
        self.prev = buf[consumed:]
        self.pos = nxt - consumed
        return out


def list_devices():
    import sounddevice as sd

    apis = sd.query_hostapis()
    print("Bemeneti eszközök:")
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0:
            print(f"  {i:3d}  {d['name']}  [{apis[d['hostapi']]['name']}]  "
                  f"{d['max_input_channels']} csatorna, {int(d['default_samplerate'])} Hz")


def pick_device(sd):
    if DEVICE_INDEX is not None:
        return DEVICE_INDEX
    apis = sd.query_hostapis()
    pref = ["Windows DirectSound", "MME", "Windows WASAPI", "Windows WDM-KS"]
    best = None
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] <= 0 or DEVICE_NAME.lower() not in d["name"].lower():
            continue
        api = apis[d["hostapi"]]["name"]
        rank = pref.index(api) if api in pref else len(pref)
        if best is None or rank < best[0]:
            best = (rank, i)
    if best is None:
        raise SystemExit(f"Nem találom a(z) '{DEVICE_NAME}' bemenetet. Nézd meg: python {SCRIPT_NAME} --list-devices")
    return best[1]


def start_capture(audio_q):
    import sounddevice as sd

    dev = pick_device(sd)
    info = sd.query_devices(dev)
    ch = max(1, min(2, info["max_input_channels"]))

    def to_mono(indata):
        return indata.mean(axis=1) if indata.shape[1] > 1 else indata[:, 0].copy()

    def cb(indata, frames, time_info, status):
        if status:
            dbg(f"hangkártya: {status}")
        audio_q.put((to_mono(indata).astype(np.float32), time.monotonic()))

    try:
        stream = sd.InputStream(device=dev, channels=ch, samplerate=SAMPLE_RATE, dtype="float32", callback=cb)
        rate_note = f"{SAMPLE_RATE} Hz"
    except Exception:
        sr = int(info["default_samplerate"])      # pl. WASAPI: csak a natív mintavétellel nyílik meg
        rs = Resampler(sr, SAMPLE_RATE)

        def cb2(indata, frames, time_info, status):
            audio_q.put((rs.process(to_mono(indata)), time.monotonic()))

        stream = sd.InputStream(device=dev, channels=ch, samplerate=sr, dtype="float32", callback=cb2)
        rate_note = f"{sr} Hz -> {SAMPLE_RATE} Hz"
    stream.start()
    print(f"Hangbemenet: [{dev}] {info['name']} ({ch} csatorna, {rate_note})", flush=True)
    return stream


def load_audio_file(path):
    """Hangfájl -> 16 kHz mono float32. WAV-hoz nem kell semmi extra csomag."""
    errors = []

    def finish(x, sr):
        x = np.asarray(x, np.float32)
        if sr != SAMPLE_RATE:
            x = Resampler(sr, SAMPLE_RATE).process(x)
        return x.astype(np.float32)

    if path.lower().endswith(".wav"):
        try:
            import wave

            with wave.open(path, "rb") as w:
                sr, ch, sw = w.getframerate(), w.getnchannels(), w.getsampwidth()
                raw = w.readframes(w.getnframes())
            if sw == 1:
                x = (np.frombuffer(raw, np.uint8).astype(np.float32) - 128) / 128
            elif sw == 2:
                x = np.frombuffer(raw, "<i2").astype(np.float32) / 32768
            elif sw == 3:
                b = np.frombuffer(raw, np.uint8).reshape(-1, 3).astype(np.int32)
                v = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
                x = np.where(v >= 1 << 23, v - (1 << 24), v).astype(np.float32) / (1 << 23)
            else:
                x = np.frombuffer(raw, "<i4").astype(np.float32) / 2147483648
            return finish(x.reshape(-1, ch).mean(axis=1), sr)
        except Exception as e:
            errors.append(f"wave: {e}")
    try:
        import soundfile as sf

        x, sr = sf.read(path, dtype="float32", always_2d=True)
        return finish(x.mean(axis=1), sr)
    except Exception as e:
        errors.append(f"soundfile: {e}")
    try:
        from faster_whisper import decode_audio

        return decode_audio(path, sampling_rate=SAMPLE_RATE)
    except Exception as e:
        errors.append(f"faster-whisper: {e}")
    raise SystemExit("Nem tudom beolvasni a hangfájlt (" + "; ".join(errors) + ").\n"
                     "Tipp: alakítsd WAV-ra, pl.: ffmpeg -i felvetel.mkv -ac 1 -ar 16000 teszt.wav")


def start_wav_feeder(path, audio_q, speed, on_done):
    audio = load_audio_file(path)
    print(f"WAV lejátszás: {path} ({len(audio) / SAMPLE_RATE:.1f} s, {speed}x sebesség)", flush=True)

    def run():
        blk = 320  # 20 ms-os darabok, mint egy hangkártyánál
        audio2 = np.concatenate([audio, np.zeros(int(1.5 * SAMPLE_RATE), np.float32)])
        t0 = time.monotonic()
        for i in range(0, len(audio2), blk):
            wait = t0 + (i + blk) / SAMPLE_RATE / speed - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            audio_q.put((audio2[i:i + blk].copy(), time.monotonic()))
        on_done()

    threading.Thread(target=run, daemon=True, name="wav").start()


# -----------------------------------------------------------------------------

def main():
    global DEBUG, MODEL_NAME, TRANSLATOR, OVERLAY, OVERLAY_SCREEN, DEVICE_NAME, DEVICE_INDEX, DEEPL_API_KEY
    ap = argparse.ArgumentParser(description="rksubbyV2 - élő magyar feliratozó (faster-whisper + DeepL + OBS)")
    ap.add_argument("--list-devices", action="store_true", help="bemeneti eszközök listája")
    ap.add_argument("--device", help="bemeneti eszköz neve (részlet) vagy sorszáma")
    ap.add_argument("--wav", help="hangfájl lejátszása valós időben élő bemenet helyett")
    ap.add_argument("--speed", type=float, default=1.0, help="WAV lejátszási sebesség")
    ap.add_argument("--model", help=f"Whisper modell (alap: {MODEL_NAME})")
    ap.add_argument("--translator", choices=["deepl", "ollama", "auto", "google", "google_web", "none"],
                    help=f"fordító (alap: {TRANSLATOR})")
    ap.add_argument("--deepl-key", nargs="?", const="", default=None, metavar="KULCS",
                    help="DeepL API-kulcs beállítása (egyszer kell; a vágólapról is beolvassa)")
    ap.add_argument("--test-translate", action="store_true", help="a fordítók gyors tesztje, Whisper nélkül")
    ap.add_argument("--overlay", action="store_true", help="beépített overlay-ablak is")
    ap.add_argument("--screen", type=int, help="melyik monitoron legyen az overlay (0, 1, 2...)")
    ap.add_argument("--no-overlay", action="store_true", help="overlay nélkül (ez az alapértelmezés)")
    ap.add_argument("--debug", action="store_true", help="beszédszakaszok kiírása")
    args = ap.parse_args()

    if args.list_devices:
        list_devices()
        return
    if args.deepl_key is not None:
        sys.exit(setup_deepl_key(args.deepl_key or None))
    if args.test_translate:
        test_translators()
        return
    DEBUG = args.debug
    if args.device:
        if args.device.isdigit():
            DEVICE_INDEX = int(args.device)
        else:
            DEVICE_NAME = args.device
    if args.model:
        MODEL_NAME = args.model
    if args.translator:
        TRANSLATOR = args.translator
    if args.overlay:
        OVERLAY = True
    if args.no_overlay:
        OVERLAY = False
    if args.screen is not None:
        OVERLAY_SCREEN = args.screen

    # Első indítás DeepL-kulcs nélkül: itt helyben bekéri (egyszer kell), utána a fájlból olvassa
    if TRANSLATOR in ("deepl", "auto") and not DEEPL_API_KEY:
        interactive = False
        try:
            interactive = sys.stdin is not None and sys.stdin.isatty()
        except Exception:
            pass
        if interactive:
            print("Még nincs beállítva DeepL-kulcs.\n", flush=True)
            if setup_deepl_key() == 0:
                DEEPL_API_KEY = load_deepl_key()
                print(flush=True)
            else:
                print("Kulcs nélkül megy tovább: angol felirat jelenik meg. "
                      f"Később: python {SCRIPT_NAME} --deepl-key\n", flush=True)

    t_start = time.perf_counter()
    print("[1/4] Könyvtárak betöltése (faster-whisper, CUDA, torch) - ez 10-40 mp is lehet...", flush=True)
    t = time.perf_counter()
    importlib.import_module("faster_whisper")
    print(f"      kész ({time.perf_counter() - t:.1f} s)", flush=True)

    def use_cpu(reason):
        global MODEL_NAME, ASR_DEVICE, COMPUTE_TYPE
        print(f"      {reason} CPU-n fut, kisebb modellel ({CPU_MODEL_NAME}): lassabb és pontatlanabb lesz.\n"
              f"      NVIDIA kártyához: python -m pip install -r requirements-gpu.txt", flush=True)
        ASR_DEVICE, COMPUTE_TYPE = "cpu", "int8"
        if not args.model:
            MODEL_NAME = CPU_MODEL_NAME

    if ASR_DEVICE == "cuda" and not cuda_available():
        use_cpu("Nem találok NVIDIA (CUDA) GPU-t.")
    print(f"[2/4] Modell betöltése: {MODEL_NAME} ({ASR_DEVICE}, {COMPUTE_TYPE})...", flush=True)
    t = time.perf_counter()
    try:
        asr = WhisperASR(MODEL_NAME, ASR_DEVICE, COMPUTE_TYPE)
    except Exception as e:
        if ASR_DEVICE != "cuda":
            raise
        use_cpu(f"A GPU-s betöltés nem sikerült ({_short_err(e)}).")
        asr = WhisperASR(MODEL_NAME, ASR_DEVICE, COMPUTE_TYPE)
    print(f"      kész ({time.perf_counter() - t:.1f} s)", flush=True)

    print(f"[3/4] {'GPU' if ASR_DEVICE == 'cuda' else 'CPU'} bemelegítése (az első futás a leglassabb)...",
          flush=True)
    t = time.perf_counter()
    asr.transcribe(np.zeros(SAMPLE_RATE, np.float32))
    print(f"      kész ({time.perf_counter() - t:.1f} s)", flush=True)

    print("[4/4] Fordító, kimenet és hangbemenet indítása...", flush=True)
    translator = Translator(TRANSLATOR)
    print(f"      Fordítók sorrendje: {translator.names()}", flush=True)
    if DEEPL_API_KEY and any(name == "deepl" for name, _ in translator.chain):
        try:
            used, limit = deepl_usage(DEEPL_API_KEY, timeout=4.0)
            print(f"      DeepL havi keret: {_fmt_int(used)} / {_fmt_int(limit)} karakter felhasználva", flush=True)
        except Exception as e:
            print(f"      DeepL: {_short_err(e)}", flush=True)
    print(f"      Fordító: {translator.probe()}", flush=True)
    display = SubtitleSink(WRITE_SUB_FILES, LOG_PATH)
    if OBS_HTTP_PORT and start_overlay_server(display, OBS_HTTP_PORT):
        print(f"      OBS-felirat: http://127.0.0.1:{OBS_HTTP_PORT}/  (böngészőforrás URL-je, "
              f"'Helyi fájl' kikapcsolva, 1920x1080)", flush=True)
    if WRITE_SUB_FILES:
        print(f"      Fájlba is: {SUB_TXT_PATH} és sub.json", flush=True)
    audio_q, display_q = queue.Queue(), queue.Queue()
    stop = threading.Event()
    sub = LiveSubtitler(asr, StreamingVAD(), translator, display, display_q)
    streams = []

    def wav_done():
        time.sleep(3.0)   # az utolsó mondat még befut
        stop.set()

    def start_workers():
        threading.Thread(target=sub.run, args=(audio_q, stop), daemon=True, name="asr").start()
        threading.Thread(target=display_loop, args=(display_q, display, stop), daemon=True, name="kijelzo").start()
        if args.wav:
            start_wav_feeder(args.wav, audio_q, args.speed, wav_done)
        else:
            streams.append(start_capture(audio_q))
        print(f"\nFUT! ({time.perf_counter() - t_start:.0f} s alatt indult)  Kilépés: Ctrl+C\n", flush=True)

    rc = None
    if OVERLAY:
        rc = run_with_overlay(display, start_workers)
    if rc is None:   # nincs overlay (vagy nem indult): egyszerű várakozó ciklus
        start_workers()
        try:
            while not stop.is_set():
                time.sleep(0.2)
        except KeyboardInterrupt:
            pass
    stop.set()
    for s in streams:
        try:
            s.stop()
            s.close()
        except Exception:
            pass
    display.clear()   # ne maradjon kint felirat az OBS-ben
    print("Kilépés...")
    sys.exit(rc or 0)


if __name__ == "__main__":
    main()
