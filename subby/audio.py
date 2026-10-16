# -*- coding: utf-8 -*-
"""Windows-hangbemenetek (PortAudio): lista, kiválasztás, élő bemenet őrrel; újramintavételezés; WAV-lejátszás."""

import threading
import time

import numpy as np

from .config import SAMPLE_RATE
from .textutil import short_err

# Ezeken az illesztőfelületeken keresünk, ebben a sorrendben (a DirectSound a teljes nevet adja,
# és 16 kHz-en is megnyílik; a WASAPI csak a natív mintavétellel)
_API_PREF = ["Windows DirectSound", "MME", "Windows WASAPI", "Windows WDM-KS"]
_SKIP_NAMES = ("primary sound capture driver", "microsoft sound mapper",        # angol Windows
               "elsődleges hangrögzítő", "microsoft hangleképző")                 # magyar Windows


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


def _api_rank(api_name):
    return _API_PREF.index(api_name) if api_name in _API_PREF else len(_API_PREF)


def list_input_devices():
    """A bemeneti eszközök, eszközönként egyszer (a legjobb illesztőfelülettel):
    [{index, name, api, channels, rate, cable}]"""
    import sounddevice as sd

    apis = sd.query_hostapis()
    all_devs = [(i, d, apis[d["hostapi"]]["name"]) for i, d in enumerate(sd.query_devices())
                if d["max_input_channels"] > 0]
    if not all_devs:
        return []
    best_api = min((api for _i, _d, api in all_devs), key=_api_rank)   # egy felület: nincs csonka név-duplikátum
    out = []
    for i, d, api in all_devs:
        if api != best_api or d["name"].strip().lower().startswith(_SKIP_NAMES):
            continue
        out.append({"index": i, "name": d["name"].strip(), "api": api, "channels": d["max_input_channels"],
                    "rate": int(d["default_samplerate"]), "cable": "cable output" in d["name"].lower()})
    return out


def print_devices():
    import sounddevice as sd

    apis = sd.query_hostapis()
    print("Bemeneti eszközök:")
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0:
            print(f"  {i:3d}  {d['name']}  [{apis[d['hostapi']]['name']}]  "
                  f"{d['max_input_channels']} csatorna, {int(d['default_samplerate'])} Hz")


def pick_device(sd, name, index=None):
    """Eszköz kiválasztása sorszám vagy név(részlet) alapján; a csonka (MME) neveket is felismeri."""
    if index is not None:
        return index
    want = (name or "").strip().lower()
    if not want:
        raise RuntimeError("nincs kiválasztva hangbemenet")
    apis = sd.query_hostapis()
    best = None
    for i, d in enumerate(sd.query_devices()):
        dn = d["name"].strip().lower()
        if d["max_input_channels"] <= 0 or not (want in dn or (len(dn) >= 12 and dn in want)):
            continue
        rank = _api_rank(apis[d["hostapi"]]["name"])
        if best is None or rank < best[0]:
            best = (rank, i)
    if best is None:
        raise RuntimeError(f"nem találom a(z) '{name}' hangbemenetet")
    return best[1]


class AudioCapture:
    """Windows-hangbemenet (VB-CABLE, USB-s capture kártya, mikrofon) a saját mintavételével, sztereóban.
    on_audio(x, rate, t): x float32 (n, 2). Az őr újranyitja a bemenetet, ha 3 mp-ig nem jön adat
    (pl. illesztőprogram-újraindulás, alvó mód után)."""

    STALL_S = 3.0

    def __init__(self, device_name, device_index, on_audio, log=print):
        self.device_name, self.device_index = device_name, device_index
        self.on_audio = on_audio
        self.log = log
        self.stream = None
        self.desc = ""
        self.ok = False
        self.error = ""
        self.last_cb = time.monotonic()
        self._stop = threading.Event()
        self._lock = threading.Lock()

    def _open(self, refresh=False):
        import sounddevice as sd

        if refresh:   # a PortAudio indításkor rögzíti az eszközlistát: eszközcsere után frissíteni kell
            try:
                sd._terminate()
                sd._initialize()
            except Exception:
                pass
        dev = pick_device(sd, self.device_name, self.device_index)
        info = sd.query_devices(dev)
        ch = max(1, min(2, info["max_input_channels"]))
        rate = int(info["default_samplerate"])

        def cb(indata, frames, time_info, status):
            self.last_cb = time.monotonic()
            x = indata if ch == 2 else np.repeat(indata, 2, axis=1)
            self.on_audio(x.copy(), rate, self.last_cb)

        self.last_cb = time.monotonic()
        stream = sd.InputStream(device=dev, channels=ch, samplerate=rate, dtype="float32", callback=cb)
        stream.start()
        self.stream = stream
        self.desc = f"[{dev}] {info['name']} ({ch} csatorna, {rate} Hz)"
        self.ok, self.error = True, ""

    def _close(self):
        s, self.stream = self.stream, None
        if s is not None:
            try:
                s.stop()
                s.close()
            except Exception:
                pass

    def start(self):
        with self._lock:
            self._open()
        threading.Thread(target=self._watchdog, daemon=True, name="hang-or").start()
        return self.desc

    def _watchdog(self):
        stalled = False
        while not self._stop.wait(1.0):
            if time.monotonic() - self.last_cb <= self.STALL_S:
                if stalled:
                    self.log(f"[hang] újra jön az adat: {self.desc}")
                    stalled = False
                self.ok, self.error = True, ""
                continue
            if not stalled:
                self.log("[hang] 3 mp óta NEM JÖN ADAT a hangbemenetről (leállt az eszköz?). Újranyitom...")
                stalled = True
            self.ok, self.error = False, "nem jön adat a hangbemenetről, újranyitás..."
            with self._lock:
                if self._stop.is_set():
                    return
                self._close()
                try:
                    self._open(refresh=True)
                    self.log(f"[hang] újranyitva: {self.desc}")
                except Exception as e:
                    self.ok, self.error = False, f"a hangbemenet nem nyitható meg: {short_err(e)}"
            self._stop.wait(4.0)

    def stop(self):
        self._stop.set()
        with self._lock:
            self._close()


# -----------------------------------------------------------------------------
# Hangfájl
# -----------------------------------------------------------------------------

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
    raise RuntimeError("nem tudom beolvasni a hangfájlt (" + "; ".join(errors) + "). "
                       "Tipp: alakítsd WAV-ra, pl.: ffmpeg -i felvetel.mkv -ac 1 -ar 16000 teszt.wav")


def start_wav_feeder(audio, audio_q, speed, stop, on_done, tap=None):
    """A betöltött hangot valós időben (vagy gyorsítva) adagolja, mintha hangkártyáról jönne.
    tap(x): ugyanez a darab máshová is (pl. a hangszóróra)."""

    def run():
        blk = 320  # 20 ms-os darabok, mint egy hangkártyánál
        audio2 = np.concatenate([audio, np.zeros(int(1.5 * SAMPLE_RATE), np.float32)])
        t0 = time.monotonic()
        for i in range(0, len(audio2), blk):
            if stop.is_set():
                return
            wait = t0 + (i + blk) / SAMPLE_RATE / speed - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            x = audio2[i:i + blk].copy()
            audio_q.put((x, time.monotonic()))
            if tap is not None and speed == 1.0:
                tap(x)
        on_done()

    threading.Thread(target=run, daemon=True, name="wav").start()
