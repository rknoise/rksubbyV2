# -*- coding: utf-8 -*-
"""A feliratozó motorja: betölti a modellt, elindítja a fordítót és a hangbemenetet, és leállítja.
A grafikus felület és a parancssori mód is ezt használja. A modell két indítás között betöltve marad."""

import os
import queue
import threading
import time

from .config import SAMPLE_RATE, TEST_WAV, load_deepl_key, load_profile
from .textutil import fmt_int, short_err


def display_loop(display_q, sink, translator, stop, timeout_s):
    """A kész mondatokat sorrendben kiírja, amint megjön a fordításuk (vagy lejár az idő)."""
    while not stop.is_set():
        try:
            item = display_q.get(timeout=0.2)
        except queue.Empty:
            continue
        try:
            hu, tr_ms = item.future.result(timeout=timeout_s + 2)
        except Exception:
            hu, tr_ms = None, -1.0
        latency = (time.monotonic() - item.t_speech_end) * 1000
        if hu:
            translator.note_shown(item.en)
        sink.final(hu or item.en, item.en, item.asr_ms, tr_ms, latency)


class Engine:
    def __init__(self, cfg, sink, log=print, debug=False):
        self.cfg, self.sink = cfg, sink
        self.log, self.debug = log, debug
        self.state = "idle"          # idle | loading | running | stopping | error
        self.mode = None             # live | test
        self.steps = []
        self.error = ""
        self.finished = ""
        self.info = {}
        self.started_at = 0.0
        self.last_stats = None       # az utolsó menet fordítási statisztikája
        self.on_before_start = None  # pl. a varázsló hangszintmérőjének leállítása
        self.capture = self.translator = self.subtitler = self.monitor = None
        self._asr = None
        self._asr_req = None
        self._asr_desc = ""
        self._lock = threading.RLock()
        self._run_id = 0
        self._stop = threading.Event()
        self._threads = []
        self._boot_thread = None

    # ------------------------------------------------------------ vezérlés
    def start(self, mode="live", wav_path=None, speed=1.0):
        with self._lock:
            if self.state in ("loading", "running", "stopping"):
                return False, "a feliratozó már fut vagy épp indul"
            if self._boot_thread and self._boot_thread.is_alive():
                return False, "az előző indítás még leáll, próbáld újra pár másodperc múlva"
            self._run_id += 1
            rid = self._run_id
            self.state, self.mode, self.error, self.finished = "loading", mode, "", ""
            self.steps, self.info = [], {}
            self._stop = threading.Event()
            self._threads = []
            t = threading.Thread(target=self._boot, args=(rid, self._stop, mode, wav_path, speed),
                                 daemon=True, name="indito")
            self._boot_thread = t
        t.start()
        return True, "indul"

    def stop(self, reason="Leállítva."):
        with self._lock:
            if self.state not in ("loading", "running"):
                return False
            self._run_id += 1            # az esetleg még futó indítás érvénytelen
            self.state = "stopping"
            stop, threads = self._stop, self._threads
            cap, tr, mon = self.capture, self.translator, self.monitor
            self.capture = self.translator = self.subtitler = self.monitor = None
        stop.set()
        if cap:
            cap.stop()
        if mon:
            mon.stop()
        if tr:
            self.last_stats = tr.snapshot()
            tr.shutdown()
        for t in threads:
            t.join(timeout=3)
        self.sink.partial("")
        self.sink.clear()
        with self._lock:
            self.state, self.finished = "idle", reason
        self.log(reason)
        return True

    def status(self):
        with self._lock:
            st = {"state": self.state, "mode": self.mode, "error": self.error, "finished": self.finished,
                  "steps": self.steps[-16:], "info": dict(self.info),
                  "uptime_s": round(time.monotonic() - self.started_at) if self.state == "running" else 0}
            sub, cap, tr, mon = self.subtitler, self.capture, self.translator, self.monitor
        if sub:
            st["audio"] = dict(sub.level, msg=sub.status_msg, lag=round(sub.lag, 2),
                               stalled=time.monotonic() - sub.last_audio_t > 3.0)
            if cap:
                st["audio"].update(ok=cap.ok, error=cap.error)
            if mon:
                st["audio"].update(monitor=mon.desc if mon.stream else "", monitor_error=mon.error)
        if tr:
            st["translator"] = tr.snapshot()
        elif self.last_stats:
            st["translator"] = self.last_stats
        return st

    # ------------------------------------------------------------ indítás
    def _step(self, msg, rid=None):
        with self._lock:
            if rid is not None and rid != self._run_id:
                return
            self.steps.append({"t": time.strftime("%H:%M:%S"), "msg": msg})
        self.log("      " + msg)

    def _boot(self, rid, stop, mode, wav_path, speed):
        t_start = time.perf_counter()
        cfg = self.cfg
        cap = translator = monitor = None
        try:
            if self.on_before_start:
                self.on_before_start()
            self._step("Könyvtárak betöltése (faster-whisper, CUDA) - első alkalommal 10-40 mp is lehet...", rid)
            from .asr import add_nvidia_dll_dirs

            add_nvidia_dll_dirs()
            import numpy as np

            from .audio import load_audio_file, start_wav_feeder
            from .capture import AsrFeeder, AudioMonitor, make_source
            from .subtitler import LiveSubtitler
            from .translate import Translator, deepl_usage
            from .vad import StreamingVAD

            asr = self._get_asr(rid, np)
            if stop.is_set():
                return

            profile = load_profile(cfg.profile)
            asr.hotwords = profile["hotwords"]
            self.info["profile"] = profile["name"]
            self._step(f"Játékprofil: {profile['name']}", rid)

            chain, key = self._build_chain(profile, rid)
            translator = Translator(chain, log=self.log)
            self.info["translator"] = translator.names()
            self._step(f"Fordítók sorrendje: {translator.names()}", rid)
            if key and any(name == "deepl" for name, _ in translator.chain):
                try:
                    used, limit = deepl_usage(key, timeout=4.0)
                    self.info["deepl_used"], self.info["deepl_limit"] = used, limit
                    self._step(f"DeepL havi keret: {fmt_int(used)} / {fmt_int(limit)} karakter felhasználva", rid)
                except Exception as e:
                    self._step(f"DeepL: {short_err(e)}", rid)
            self._step(f"Fordító: {translator.probe()}", rid)
            if stop.is_set():
                translator.shutdown()
                return

            audio_q, display_q = queue.Queue(), queue.Queue()
            sub = LiveSubtitler(cfg, asr, StreamingVAD(), translator, self.sink, display_q,
                                log=self.log, debug=self.debug)
            threads = [threading.Thread(target=sub.run, args=(audio_q, stop), daemon=True, name="asr"),
                       threading.Thread(target=display_loop, daemon=True, name="kijelzo",
                                        args=(display_q, self.sink, translator, stop, cfg.translate_timeout_s))]
            monitor = AudioMonitor(cfg, log=self.log)    # a hang a hangszórón is szól (ha be van kapcsolva)
            if mode == "test":
                path = wav_path or TEST_WAV
                audio = load_audio_file(path)
                self.info["input"] = f"próbafelvétel: {os.path.basename(path)} ({len(audio) / SAMPLE_RATE:.1f} s)"
            else:
                feeder = AsrFeeder(audio_q)

                def on_audio(x, rate, t):
                    feeder(x, rate, t)
                    monitor.push(x, rate)

                cap = make_source(cfg, on_audio, log=self.log)
                self.info["input"] = cap.start()
            self._step(f"Hangbemenet: {self.info['input']}", rid)

            with self._lock:
                if rid != self._run_id or stop.is_set():
                    raise _Cancelled()
                for t in threads:
                    t.start()
                self._threads = threads
                self.capture, self.translator, self.subtitler, self.monitor = cap, translator, sub, monitor
                self.state, self.started_at = "running", time.monotonic()
            if mode == "test":
                start_wav_feeder(audio, audio_q, speed, stop, lambda: self._wav_done(rid),
                                 tap=lambda x: monitor.push(np.repeat(x[:, None], 2, axis=1), SAMPLE_RATE))
            self._step(f"FUT! ({time.perf_counter() - t_start:.0f} s alatt indult)", rid)
        except _Cancelled:
            for obj in (cap, monitor):
                if obj:
                    obj.stop()
            if translator:
                translator.shutdown()
        except Exception as e:
            for obj in (cap, monitor):
                if obj:
                    obj.stop()
            if translator:
                translator.shutdown()
            stop.set()
            with self._lock:
                if rid == self._run_id:
                    self.state, self.error = "error", short_err(e)
            self.log(f"[hiba] Az indítás nem sikerült: {short_err(e)}")

    def _build_chain(self, profile, rid=None):
        """A fordítósor a beállítások, a DeepL-kulcs és a profil glosszáriuma alapján."""
        from .translate import build_chain, ensure_deepl_glossary

        cfg = self.cfg
        key = load_deepl_key()
        glossary_id = None
        if key and cfg.translator in ("deepl", "auto") and cfg.deepl_glossary and profile["glossary"]:
            try:
                glossary_id, msg = ensure_deepl_glossary(key, profile["id"], profile["glossary"])
                self._step(f"DeepL {msg}", rid)
            except Exception as e:
                self._step(f"DeepL-glosszárium nélkül megy tovább ({short_err(e)})", rid)
        return build_chain(cfg, profile, key, glossary_id, log=self.log), key

    def reload_translator(self):
        """Új DeepL-kulcs vagy fordítóbeállítás után: a futó feliratozó fordítója újraindítás nélkül cserélődik."""
        tr = self.translator
        if tr is None:
            return False

        def work():
            try:
                chain, _key = self._build_chain(load_profile(self.cfg.profile))
            except Exception as e:
                self.log(f"[fordító] frissítés nem sikerült: {short_err(e)}")
                return
            tr.set_chain(chain)
            self.info["translator"] = tr.names()
            self._step(f"Fordító frissítve: {tr.names()}")

        threading.Thread(target=work, daemon=True, name="fordito-frissites").start()
        return True

    def _get_asr(self, rid, np):
        from .asr import WhisperASR, cuda_device_count

        cfg = self.cfg
        device = cfg.asr_device
        if device == "auto":
            device = "cuda" if cuda_device_count() > 0 else "cpu"
            if device == "cpu":
                self._step("Nem találok NVIDIA (CUDA) GPU-t: CPU-n fut, kisebb modellel (lassabb, pontatlanabb).", rid)
        model = cfg.model if device == "cuda" else cfg.cpu_model
        compute = "float16" if device == "cuda" else "int8"
        req = (model, device, compute)
        if self._asr is not None and self._asr_req == req:
            self._step(f"Felismerő modell: {self._asr_desc} (már betöltve)", rid)
            self.info["model"] = self._asr_desc
            return self._asr
        self._asr = None
        self._step(f"Modell betöltése: {model} ({'GPU' if device == 'cuda' else 'CPU'}, {compute})...", rid)

        def on_download():
            self._step("A modell még nincs letöltve: letöltöm (egyszeri, kb. 1,5 GB, pár perc)...", rid)

        try:
            asr = WhisperASR(model, device, compute, on_download=on_download)
        except Exception as e:
            if device != "cuda":
                raise
            self._step(f"A GPU-s betöltés nem sikerült ({short_err(e)}). CPU-n fut, kisebb modellel. "
                       "NVIDIA-hoz telepítsd a GPU-csomagokat (Telepítés lépés).", rid)
            device, compute, model = "cpu", "int8", cfg.cpu_model
            asr = WhisperASR(model, device, compute, on_download=on_download)
        self._step("Bemelegítés (az első futás a leglassabb)...", rid)
        asr.transcribe(np.zeros(SAMPLE_RATE, np.float32))
        self._asr, self._asr_req = asr, req
        self._asr_desc = f"{model} ({'GPU' if device == 'cuda' else 'CPU'})"
        self.info["model"] = self._asr_desc
        return asr

    def _wav_done(self, rid):
        def later():
            time.sleep(3.0)   # az utolsó mondat még befut
            if rid == self._run_id:
                self.stop(reason="A próba lefutott.")

        threading.Thread(target=later, daemon=True).start()


class _Cancelled(Exception):
    pass
