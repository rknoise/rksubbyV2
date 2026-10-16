# -*- coding: utf-8 -*-
"""Beszédfelismerés: faster-whisper (GPU vagy CPU), GPU-felismerés, modell-gyorsítótár."""

import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field

from .textutil import clean_spaces

_added_dll_dirs = set()


def add_nvidia_dll_dirs():
    """A pip-pel telepített nvidia-* csomagok DLL-jei (Windows Store-os Pythonhoz is).
    Többször is hívható: csak az új mappákat veszi fel (pl. a GPU-csomagok telepítése után)."""
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
            if os.path.basename(root).lower() != "bin" or root in _added_dll_dirs:
                continue
            _added_dll_dirs.add(root)
            try:
                os.add_dll_directory(root)
            except Exception:
                pass
            os.environ["PATH"] = root + os.pathsep + os.environ.get("PATH", "")


def cuda_device_count():
    try:
        add_nvidia_dll_dirs()
        import ctranslate2

        return ctranslate2.get_cuda_device_count()
    except Exception:
        return 0


def gpu_name():
    """Az NVIDIA kártya neve (nvidia-smi), vagy üres, ha nincs."""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return ""
    try:
        out = subprocess.run([exe, "--query-gpu=name,memory.total", "--format=csv,noheader"],
                             capture_output=True, text=True, timeout=5,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        line = out.stdout.strip().splitlines()[0] if out.returncode == 0 and out.stdout.strip() else ""
        return line.replace(",", " ·", 1)
    except Exception:
        return ""


def model_is_cached(name):
    """Le van-e már töltve a felismerő modell (a Hugging Face gyorsítótárban)."""
    if os.path.isdir(name):
        return True
    try:
        from faster_whisper.utils import download_model

        download_model(name, local_files_only=True)
        return True
    except Exception:
        return False


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


class WhisperASR:
    def __init__(self, model_name, device, compute_type, language="en", on_download=None):
        from faster_whisper import WhisperModel

        self.model_name, self.device, self.compute_type = model_name, device, compute_type
        self.language = language
        self.hotwords = ""            # játékprofilonként változik, újratöltés nélkül
        try:
            # Először csak a helyi gyorsítótárból: nem kérdezi le minden indításkor a Hugging Face-t
            self.model = WhisperModel(model_name, device=device, compute_type=compute_type, local_files_only=True)
        except Exception as e:
            if not any(c.__name__ in ("LocalEntryNotFoundError", "FileNotFoundError") for c in type(e).__mro__):
                raise
            if on_download:
                on_download()
            self.model = WhisperModel(model_name, device=device, compute_type=compute_type)

    def transcribe(self, audio, beam_size=1, word_timestamps=False, prompt=None):
        import numpy as np

        t0 = time.perf_counter()
        segments, _info = self.model.transcribe(
            audio,
            language=self.language,
            task="transcribe",
            beam_size=beam_size,
            temperature=0.0,                    # nincs lassú "újrapróbálás" magasabb hőmérséklettel
            condition_on_previous_text=False,
            initial_prompt=prompt or None,
            hotwords=self.hotwords or None,
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
