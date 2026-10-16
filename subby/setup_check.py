# -*- coding: utf-8 -*-
"""Rendszerellenőrzés a beállító varázslóhoz (Python, csomagok, GPU, VB-CABLE, modell, fordítók)
és a csomagok telepítése a felületről (pip, a háttérben, élő naplóval). A modul csak stdlib-et tölt be."""

import collections
import importlib
import importlib.metadata
import importlib.util
import os
import subprocess
import sys
import threading
import time

from .config import BASE_DIR, deepl_key_source

BASE_PACKAGES = [("faster-whisper", "faster_whisper"), ("sounddevice", "sounddevice"), ("numpy", "numpy"),
                 ("requests", "requests"), ("onnxruntime", "onnxruntime"), ("ctranslate2", "ctranslate2"),
                 ("imageio-ffmpeg", "imageio_ffmpeg")]
GPU_PACKAGES = ["nvidia-cublas-cu12", "nvidia-cudnn-cu12"]


def _dist_version(name):
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def check_packages():
    importlib.invalidate_caches()
    out = []
    for dist, mod in BASE_PACKAGES:
        ok = importlib.util.find_spec(mod) is not None
        out.append({"name": dist, "ok": ok, "version": _dist_version(dist) if ok else None})
    return out


def run_checks(cfg):
    """Összesítő a beállító varázsló első lépéséhez. Néhány másodpercig is eltarthat."""
    t0 = time.perf_counter()
    res = {"python": {"version": sys.version.split()[0], "exe": sys.executable,
                      "ok": (3, 10) <= sys.version_info[:2] <= (3, 13), "bits": 64 if sys.maxsize > 2**32 else 32}}
    pkgs = check_packages()
    res["packages"] = pkgs
    res["base_ok"] = all(p["ok"] for p in pkgs)
    res["pyqt5"] = importlib.util.find_spec("PyQt5") is not None
    res["path_ascii"] = BASE_DIR.isascii()

    from .asr import cuda_device_count, gpu_name, model_is_cached

    name = gpu_name()
    gpu_libs = all(_dist_version(p) for p in GPU_PACKAGES)
    cuda = cuda_device_count() if importlib.util.find_spec("ctranslate2") is not None else 0
    res["gpu"] = {"name": name, "nvidia": bool(name), "cuda_devices": cuda, "libs": gpu_libs,
                  "ok": bool(name) and gpu_libs and cuda > 0}

    uses_gpu = cfg.asr_device == "cuda" or (cfg.asr_device == "auto" and cuda > 0)
    model = cfg.model if uses_gpu else cfg.cpu_model
    res["model"] = {"name": model, "cached": model_is_cached(model) if res["base_ok"] else False}

    sources, src_errors, ffmpeg = [], [], ""
    if importlib.util.find_spec("sounddevice") is not None and importlib.util.find_spec("numpy") is not None:
        try:
            from .capture import find_ffmpeg, list_audio_sources

            ffmpeg = find_ffmpeg(cfg)
            sources, src_errors = list_audio_sources(cfg)
        except Exception as e:
            src_errors = [str(e)]
    want = (cfg.dshow_device if cfg.audio_kind == "dshow" else cfg.device_name or "").lower()
    res["audio"] = {"sources": len(sources), "cable": any(d["cable"] for d in sources),
                    "cards": [d["name"] for d in sources if d["card"]], "error": "; ".join(src_errors),
                    "selected_found": bool(want) and any(d["kind"] == cfg.audio_kind and want in d["name"].lower()
                                                         for d in sources)}
    res["ffmpeg"] = {"path": ffmpeg, "ok": bool(ffmpeg)}
    from .windows import browser_exe

    res["browser"] = {"path": browser_exe() or "", "ok": bool(browser_exe())}

    res["deepl"] = {"key": deepl_key_source()}
    if importlib.util.find_spec("requests") is not None:
        from .translate import ollama_status

        running, omodel = ollama_status(cfg.ollama_url, cfg.ollama_model)
        res["ollama"] = {"running": running, "model": omodel}
    else:
        res["ollama"] = {"running": False, "model": None}
    res["ms"] = round((time.perf_counter() - t0) * 1000)
    return res


class Installer:
    """pip install a háttérben; a napló sorai a felületen élőben látszanak."""

    def __init__(self):
        self.running = False
        self.what = ""
        self.rc = None
        self.lines = collections.deque(maxlen=400)
        self._lock = threading.Lock()

    def start(self, what):
        req = {"base": "requirements.txt", "gpu": "requirements-gpu.txt", "pyqt5": None}.get(what, "")
        if req == "":
            return False, "ismeretlen csomagcsoport"
        with self._lock:
            if self.running:
                return False, "már fut egy telepítés"
            self.running, self.what, self.rc = True, what, None
            self.lines.clear()
        args = ["-r", os.path.join(BASE_DIR, req)] if req else ["PyQt5"]
        cmd = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", *args]
        threading.Thread(target=self._run, args=(cmd,), daemon=True, name="telepito").start()
        return True, "indul"

    def _run(self, cmd):
        self.lines.append("> " + " ".join(os.path.basename(c) if i == 0 else c for i, c in enumerate(cmd)))
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
        try:
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                 encoding="utf-8", errors="replace", env=env, cwd=BASE_DIR,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            for line in p.stdout:
                line = line.rstrip()
                if line:
                    self.lines.append(line)
            rc = p.wait()
        except Exception as e:
            self.lines.append(f"HIBA: {e}")
            rc = -1
        importlib.invalidate_caches()
        if rc == 0:
            from .asr import add_nvidia_dll_dirs

            add_nvidia_dll_dirs()       # az új GPU-könyvtárak a program újraindítása nélkül is látszanak
            self.lines.append("Kész, a telepítés sikerült.")
        else:
            self.lines.append(f"A telepítés nem sikerült (kilépési kód: {rc}). Lásd a fenti üzeneteket.")
        with self._lock:
            self.running, self.rc = False, rc

    def status(self):
        with self._lock:
            return {"running": self.running, "what": self.what, "rc": self.rc, "log": list(self.lines)[-80:]}
