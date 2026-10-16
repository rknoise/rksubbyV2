# -*- coding: utf-8 -*-
"""Capture kártyák hangja ffmpeg-gel (DirectShow), lejátszás a hangszórón, hangszintmérő.

Egyes kártyák (pl. AVerMedia Live Gamer 4K / GC573) hangja nem külön Windows-hangeszköz, hanem a
kártya DirectShow-eszközén jön a képpel együtt. Ezt az ffmpeg tudja kiolvasni: a képet eldobja, a hangot
48 kHz sztereóban adja (10 ms-os darabokban), ebből dolgozik a felismerő és a hangszóró is.
A kártyát közben a lejátszóablak is használhatja (a kép onnan jön)."""

import collections
import os
import re
import shutil
import subprocess
import threading
import time

import numpy as np

from .audio import AudioCapture, Resampler
from .config import SAMPLE_RATE
from .textutil import short_err

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_DEV_RE = re.compile(r'\]\s+"(?P<name>[^"]+)"(?:\s+\((?P<types>[^)]*)\))?\s*$')
_dshow_cache = {"t": 0.0, "devices": [], "known": {}}


def find_ffmpeg(cfg=None):
    """Az ffmpeg helye: a beállításban megadott, a telepítővel érkezett (imageio-ffmpeg), vagy a PATH-ból."""
    if cfg is not None and cfg.ffmpeg_path and os.path.isfile(cfg.ffmpeg_path):
        return cfg.ffmpeg_path
    try:
        import imageio_ffmpeg

        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and os.path.isfile(exe):
            return exe
    except Exception:
        pass
    return shutil.which("ffmpeg") or ""


def parse_dshow_list(text):
    """Az "ffmpeg -list_devices true -f dshow" kimenete -> [{"name", "types": {"audio", "video"}}]."""
    out, section = [], None
    for line in text.splitlines():
        if "DirectShow video devices" in line:
            section = "video"
            continue
        if "DirectShow audio devices" in line:
            section = "audio"
            continue
        if "Alternative name" in line:
            continue
        m = _DEV_RE.search(line)
        if not m:
            continue
        if m.group("types") is not None:
            types = {t.strip() for t in m.group("types").split(",")} - {"none", ""}
        else:
            types = {section} if section else set()
        out.append({"name": m.group("name"), "types": types})
    return out


def list_dshow_devices(exe, max_age=20.0):
    """A DirectShow-eszközök (gyorsítótárazva). Egy épp használt eszköz típusa "(none)" is lehet: ilyenkor
    a korábban látott típusát adja vissza."""
    now = time.monotonic()
    if now - _dshow_cache["t"] < max_age and _dshow_cache["devices"]:
        return _dshow_cache["devices"]
    if not exe:
        return []
    try:
        r = subprocess.run([exe, "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
                           capture_output=True, timeout=15, creationflags=_NO_WINDOW)
        text = (r.stderr or b"").decode("utf-8", errors="replace")
    except Exception:
        return _dshow_cache["devices"]
    devices = parse_dshow_list(text)
    for d in devices:
        known = _dshow_cache["known"]
        if d["types"]:
            known[d["name"]] = d["types"]
        else:
            d["types"] = known.get(d["name"], set())
    _dshow_cache.update(t=now, devices=devices)
    return devices


# -----------------------------------------------------------------------------
# A forrás hangja: ffmpeg (DirectShow kép+hang eszköz)
# -----------------------------------------------------------------------------

class FfmpegAudioCapture:
    """A capture kártya hangja ffmpeg-gel. on_audio(x, rate, t): x float32 (n, 2) sztereó.
    Ha az ffmpeg leáll vagy elakad, újraindítja."""

    RATE = 48000
    STALL_S = 4.0       # ennyi ideig nem jön adat (az első adat után): újraindítás
    FIRST_S = 10.0      # az első adatra ennyit vár (a kártya megnyitása 1-2 mp)

    def __init__(self, exe, device, on_audio, log=print):
        self.exe, self.device, self.on_audio, self.log = exe, device, on_audio, log
        self.desc = f"{device} (DirectShow, ffmpeg, 48 kHz)"
        self.ok = False
        self.error = ""
        self.last_data = 0.0
        self._small_ok = None           # megy-e a kis felbontású képcsatorna (kevesebb fölösleges adat)
        self._stop = threading.Event()
        self._proc = None
        self._err = collections.deque(maxlen=20)
        self._started = 0.0

    def _cmd(self, small):
        c = [self.exe, "-hide_banner", "-nostdin", "-loglevel", "error", "-f", "dshow", "-rtbufsize", "64M",
             "-audio_buffer_size", "20"]
        if small:
            c += ["-video_size", "640x480"]   # a képet úgyis eldobjuk: minél kevesebb adat, annál jobb
        c += ["-i", f"video={self.device}:audio={self.device}", "-map", "0:a:0", "-ac", "2",
              "-ar", str(self.RATE), "-f", "s16le", "-flush_packets", "1", "pipe:1"]
        return c

    def start(self):
        if not self.exe:
            raise RuntimeError("nincs ffmpeg (a telepítő felteszi; vagy: pip install imageio-ffmpeg)")
        threading.Thread(target=self._run, daemon=True, name="ffmpeg-hang").start()
        threading.Thread(target=self._watchdog, daemon=True, name="ffmpeg-or").start()
        return self.desc

    def _run(self):
        while not self._stop.is_set():
            small = self._small_ok is not False
            self._err.clear()
            try:
                self._proc = subprocess.Popen(self._cmd(small), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                              bufsize=0, creationflags=_NO_WINDOW)
            except OSError as e:
                self.ok, self.error = False, f"az ffmpeg nem indul: {short_err(e)}"
                self._stop.wait(5.0)
                continue
            self._started = time.monotonic()
            threading.Thread(target=self._read_err, args=(self._proc,), daemon=True).start()
            got = self._pump(self._proc)
            try:
                self._proc.kill()
            except OSError:
                pass
            if self._stop.is_set():
                break
            if got and small:
                self._small_ok = True
            if not got and small and self._small_ok is None:
                self._small_ok = False      # kis felbontással nem nyílt meg: a kártya alapbeállításával próbálja
                continue
            err = " ".join(self._err) or "az ffmpeg kilépett"
            self.ok, self.error = False, f"a hangforrás leállt ({err[-160:]}), újraindítás..."
            self.log(f"[hang] {self.error}")
            self._stop.wait(2.0)

    def _read_err(self, proc):
        try:
            for line in proc.stderr:
                line = line.decode("utf-8", errors="replace").strip()
                if line and "Could not find audio only device" not in line:
                    self._err.append(line)
        except (OSError, ValueError):
            pass

    def _pump(self, proc):
        got, rem = 0, b""
        while not self._stop.is_set():
            try:
                b = proc.stdout.read(19200)        # legfeljebb 100 ms; a pipe annyit ad, amennyi van
            except (OSError, ValueError):
                break
            if not b:
                break
            now = time.monotonic()
            if not got:
                self.log(f"[hang] jön a hang: {self.desc}")
            got += len(b)
            self.last_data = now
            self.ok, self.error = True, ""
            data = rem + b
            n = len(data) // 4 * 4
            rem = data[n:]
            if n:
                x = np.frombuffer(data[:n], np.int16).reshape(-1, 2).astype(np.float32) / 32768.0
                try:
                    self.on_audio(x, self.RATE, now)
                except Exception as e:   # a fogyasztó hibája ne állítsa le a hangot
                    self.log(f"[hang] feldolgozási hiba: {e!r}")
        return got

    def _watchdog(self):
        while not self._stop.wait(1.0):
            p = self._proc
            if p is None or p.poll() is not None:
                continue
            now = time.monotonic()
            limit = self.STALL_S if self.last_data > self._started else self.FIRST_S
            since = now - max(self.last_data, self._started)
            if since > limit:
                self.ok = False
                self.error = "nem jön adat a kártyáról (foglalt, vagy nincs jel?), újraindítás..."
                self.log(f"[hang] {self.error}")
                try:
                    p.kill()
                except OSError:
                    pass

    def stop(self):
        self._stop.set()
        p = self._proc
        if p is not None:
            try:
                p.kill()
            except OSError:
                pass


# -----------------------------------------------------------------------------
# Lejátszás a hangszórón
# -----------------------------------------------------------------------------

def list_output_devices():
    """Kimeneti eszközök (WASAPI-nevek, ha van): [{"name", "default"}]"""
    import sounddevice as sd

    apis = sd.query_hostapis()
    api_idx = next((i for i, a in enumerate(apis) if a["name"] == "Windows WASAPI"), None)
    if api_idx is None:
        api_idx = sd.default.hostapi if isinstance(sd.default.hostapi, int) else 0
    default = apis[api_idx].get("default_output_device", -1)
    out = []
    for i, d in enumerate(sd.query_devices()):
        if d["hostapi"] == api_idx and d["max_output_channels"] > 0:
            out.append({"name": d["name"].strip(), "default": i == default})
    return out


class AudioMonitor:
    """A forrás hangja a hangszórón, kis késleltetéssel. A hangerő és a némítás menet közben is állítható
    (cfg.monitor_volume, cfg.monitor_audio). Ha a puffer túl nagyra nő, a régit eldobja."""

    MAX_S = 0.12
    START_S = 0.03

    def __init__(self, cfg, log=print):
        self.cfg, self.log = cfg, log
        self.rate = None
        self.stream = None
        self.desc = ""
        self.error = ""
        self._retry_at = 0.0
        self._buf = collections.deque()
        self._n = 0
        self._primed = False
        self._lock = threading.Lock()

    def push(self, x, rate):
        if not self.cfg.monitor_audio:
            if self.stream is not None:
                self.stop()             # a hangkártyát se foglalja feleslegesen
            return
        if self.stream is None or rate != self.rate:
            if time.monotonic() < self._retry_at:
                return
            self._open(rate)
            if self.stream is None:
                return
        with self._lock:
            self._buf.append(x)
            self._n += len(x)
            while self._n > self.MAX_S * rate and len(self._buf) > 1:
                self._n -= len(self._buf.popleft())

    def _candidates(self, sd):
        """(eszköz, extra_settings) próbálkozási sorrend: WASAPI (kis késés), DirectSound, alapértelmezett."""
        apis = sd.query_hostapis()
        want = (self.cfg.monitor_device or "").lower()
        out = []
        for api_name in ("Windows WASAPI", "Windows DirectSound"):
            idx = next((i for i, a in enumerate(apis) if a["name"] == api_name), None)
            if idx is None:
                continue
            dev = None
            if want:
                dev = next((i for i, d in enumerate(sd.query_devices())
                            if d["hostapi"] == idx and d["max_output_channels"] > 0 and want in d["name"].lower()), None)
            if dev is None:
                dev = apis[idx].get("default_output_device", -1)
            if dev is None or dev < 0:
                continue
            extra = None
            if api_name == "Windows WASAPI":
                try:
                    extra = sd.WasapiSettings(auto_convert=True)
                except Exception:
                    continue
            out.append((dev, extra))
        out.append((None, None))
        return out

    def _open(self, rate):
        import sounddevice as sd

        self.stop()
        last = None
        for dev, extra in self._candidates(sd):
            try:
                s = sd.OutputStream(samplerate=rate, channels=2, dtype="float32", latency="low", device=dev,
                                    extra_settings=extra, callback=self._cb)
                s.start()
            except Exception as e:
                last = e
                continue
            self.stream, self.rate, self.error = s, rate, ""
            info = sd.query_devices(s.device)
            self.desc = info["name"]
            return
        self.error = f"a hang nem játszható le: {short_err(last)}"
        self.log(f"[hang] {self.error}")
        self._retry_at = time.monotonic() + 10.0

    def _cb(self, outdata, frames, time_info, status):
        with self._lock:
            if not self._primed:
                if self._n < self.START_S * (self.rate or 48000):
                    outdata.fill(0)
                    return
                self._primed = True
            filled = 0
            while filled < frames and self._buf:
                chunk = self._buf[0]
                take = min(frames - filled, len(chunk))
                outdata[filled:filled + take] = chunk[:take]
                if take == len(chunk):
                    self._buf.popleft()
                else:
                    self._buf[0] = chunk[take:]
                self._n -= take
                filled += take
            if filled < frames:
                outdata[filled:] = 0
                self._primed = False    # kifogyott: újra gyűjt egy kicsit, hogy ne recsegjen
        vol = self.cfg.monitor_volume
        if vol != 1.0:
            outdata *= vol

    def stop(self):
        s, self.stream = self.stream, None
        if s is not None:
            try:
                s.stop()
                s.close()
            except Exception:
                pass
        with self._lock:
            self._buf.clear()
            self._n = 0
            self._primed = False


# -----------------------------------------------------------------------------
# A felismerő bemenete, forrásválasztás, hangszintmérő
# -----------------------------------------------------------------------------

class AsrFeeder:
    """A forrás hangja -> 16 kHz mono a felismerőnek (audio_q)."""

    def __init__(self, audio_q):
        self.q = audio_q
        self.rs, self.rate = None, None

    def __call__(self, x, rate, t):
        mono = x.mean(axis=1) if x.ndim == 2 else x
        if rate != SAMPLE_RATE:
            if self.rate != rate:
                self.rs, self.rate = Resampler(rate, SAMPLE_RATE), rate
            mono = self.rs.process(mono)
        self.q.put((mono.astype(np.float32, copy=False), t))


def make_source(cfg, on_audio, log=print):
    """A beállított hangforrás (még nem indítva)."""
    if cfg.audio_kind == "dshow":
        if not cfg.dshow_device:
            raise RuntimeError("nincs kiválasztva capture kártya (2. lépés: Forrás)")
        return FfmpegAudioCapture(find_ffmpeg(cfg), cfg.dshow_device, on_audio, log=log)
    return AudioCapture(cfg.device_name, cfg.device_index, on_audio, log=log)


def list_audio_sources(cfg):
    """A választható hangforrások: a capture kártyák (kép + hang, ffmpeg) és a Windows-hangbemenetek."""
    out, errors = [], []
    exe = find_ffmpeg(cfg)
    if exe:
        for d in list_dshow_devices(exe):
            if {"audio", "video"} <= d["types"]:
                out.append({"kind": "dshow", "name": d["name"], "card": True, "cable": False})
    else:
        errors.append("nincs ffmpeg: a képpel együtt érkező kártyahang nem érhető el")
    try:
        from .audio import list_input_devices

        for d in list_input_devices():
            out.append({"kind": "pa", "name": d["name"], "card": False, "cable": d["cable"]})
    except Exception as e:
        errors.append(short_err(e))
    return out, errors


class LevelMeter:
    """Hangszintmérő a beállító varázslóhoz; közben a hang a hangszórón is szól (ha be van kapcsolva)."""

    def __init__(self, cfg, log=print):
        self.cfg, self.log = cfg, log
        self.src = None
        self.key = None
        self.monitor = None
        self.desc = ""
        self.error = ""
        self.rms_db = self.peak_db = -120.0
        self.t = 0.0
        self._lock = threading.Lock()

    def start(self):
        cfg = self.cfg
        key = (cfg.audio_kind, cfg.dshow_device if cfg.audio_kind == "dshow" else cfg.device_name)
        with self._lock:
            if self.src is not None and self.key == key:
                return
            self._close()
            self.key, self.error, self.desc = key, "", key[1]
            self.rms_db = self.peak_db = -120.0
            self.monitor = AudioMonitor(cfg, log=self.log)
            try:
                self.src = make_source(cfg, self._on_audio, log=self.log)
                self.src.start()
            except Exception as e:
                self.src = None
                self.error = short_err(e)

    def _on_audio(self, x, rate, t):
        rms = 10 * np.log10(max(float(np.mean(x * x)), 1e-12))
        peak = 20 * np.log10(max(float(np.max(np.abs(x))) if len(x) else 0.0, 1e-6))
        # gyors felfutás, lassú lecsengés: jobban olvasható a kijelzőn
        self.rms_db = rms if rms > self.rms_db else self.rms_db * 0.9 + rms * 0.1
        self.peak_db = peak if peak > self.peak_db else self.peak_db - 0.6
        self.t = time.monotonic()
        if self.monitor:
            self.monitor.push(x, rate)

    def _close(self):
        src, self.src = self.src, None
        if src is not None:
            src.stop()
        if self.monitor is not None:
            self.monitor.stop()
            self.monitor = None

    def stop(self):
        with self._lock:
            self._close()
            self.key = None

    @property
    def active(self):
        return self.src is not None

    def level(self):
        fresh = time.monotonic() - self.t < 1.5
        src = self.src
        err = self.error or (getattr(src, "error", "") if src else "")
        return {"active": self.active, "device": self.desc, "error": err,
                "rms_db": round(self.rms_db, 1) if fresh else -120.0,
                "peak_db": round(self.peak_db, 1) if fresh else -120.0,
                "receiving": self.active and fresh,
                "monitor": self.monitor.desc if self.monitor and self.monitor.stream else "",
                "monitor_error": self.monitor.error if self.monitor else ""}


__all__ = ["AsrFeeder", "AudioMonitor", "FfmpegAudioCapture", "LevelMeter", "find_ffmpeg", "list_audio_sources",
           "list_dshow_devices", "list_output_devices", "make_source", "parse_dshow_list"]
