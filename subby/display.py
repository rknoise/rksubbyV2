# -*- coding: utf-8 -*-
"""A látható feliratok állapota: konzol, napló (sub_log.tsv), SRT-mentés, OBS-állapot (/state, /events),
régi fájlos kimenet (sub.txt / sub.json) és a Qt-overlay visszahívásai. Csak stdlib."""

import collections
import json
import os
import queue
import threading
import time

from .config import BASE_DIR, SRT_DIR

SUB_TXT_PATH = os.path.join(BASE_DIR, "sub.txt")
SUB_JSON_PATH = os.path.join(BASE_DIR, "sub.json")
LOG_PATH = os.path.join(BASE_DIR, "sub_log.tsv")


def display_seconds(text, cfg):
    return min(cfg.sub_max_s, max(cfg.sub_min_s, len(text) / max(1, cfg.read_chars_per_s)))


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
    except OSError:
        pass


def srt_time(s):
    ms = int(round(max(0.0, s) * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    sec, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{sec:02d},{ms:03d}"


class SrtWriter:
    """Feliratok mentése .srt fájlba, a mentés indításától számított időkkel (pl. OBS-felvételhez)."""

    def __init__(self, folder=SRT_DIR):
        os.makedirs(folder, exist_ok=True)
        self.path = os.path.join(folder, time.strftime("felirat_%Y-%m-%d_%H-%M-%S.srt"))
        self.f = open(self.path, "w", encoding="utf-8-sig", buffering=1)   # BOM: a Windowsos lejátszóknak
        self.t0 = time.monotonic()
        self.n = 0

    def add(self, text, t_show, duration):
        start = t_show - self.t0
        self.n += 1
        self.f.write(f"{self.n}\n{srt_time(start)} --> {srt_time(start + duration)}\n{text}\n\n")

    def close(self):
        try:
            self.f.close()
        except OSError:
            pass


class SubtitleSink:
    def __init__(self, cfg, log_path=LOG_PATH, echo=print):
        self.cfg = cfg
        self.echo = echo
        self.cond = threading.Condition()
        self.lines = []          # {"id", "hu", "en", "until"}
        self.next_id = 1
        self.partial_text, self.partial_until = "", 0.0
        self.version = 1
        self.paused = False
        self.history = collections.deque(maxlen=80)   # a vezérlőpultnak
        self.on_final = None     # Qt-overlay visszahívás (hu, en)
        self.on_partial = None   # Qt-overlay visszahívás (en)
        self.log_path = log_path
        self._log = None
        self._log_failed = False
        self.srt = None
        self._files_written = None
        self._files_q = queue.Queue()
        threading.Thread(target=self._ticker, daemon=True, name="felirat-ido").start()

    # ------------------------------------------------------------ bemenetek
    def partial(self, en):
        if not self.cfg.show_partial_en:
            en = ""
        if self.on_partial:
            self.on_partial(en)
        with self.cond:
            if not en and not self.partial_text:
                return
            self.partial_text = "" if self.paused else (en or "")
            self.partial_until = time.monotonic() + 3.0
            self._bump()

    def final(self, hu, en, asr_ms, tr_ms, latency_ms):
        self.echo(f"EN: {en}\nHU: {hu}\n    (ASR {asr_ms:.0f} ms, fordítás {tr_ms:.0f} ms, "
                  f"késés a mondat végétől: {latency_ms:.0f} ms)\n")
        now = time.monotonic()
        dur = display_seconds(hu, self.cfg)
        self._write_log(en, hu, asr_ms, tr_ms, latency_ms)
        with self.cond:
            self.history.append({"t": time.strftime("%H:%M:%S"), "en": en, "hu": hu, "asr_ms": round(asr_ms),
                                 "tr_ms": round(tr_ms), "lat_ms": round(latency_ms)})
            if self.srt:
                try:
                    self.srt.add(hu, now, dur)
                except OSError:
                    pass
            if self.paused:
                return
            self.lines.append({"id": self.next_id, "hu": hu, "en": en, "until": now + dur})
            self.next_id += 1
            del self.lines[:-max(1, self.cfg.max_lines)]
            self.partial_text = ""
            self._bump()
        if self.on_final:
            self.on_final(hu, en)

    def clear(self):
        with self.cond:
            self.lines, self.partial_text = [], ""
            self._bump()

    def set_paused(self, paused):
        with self.cond:
            self.paused = bool(paused)
            if self.paused:
                self.lines, self.partial_text = [], ""
            self._bump()

    def touch(self):
        """A kinézet vagy a megjelenítési beállítás változott: az OBS-oldal frissüljön."""
        with self.cond:
            del self.lines[:-max(1, self.cfg.max_lines)]
            self._bump()

    # ------------------------------------------------------------ SRT
    def start_srt(self):
        with self.cond:
            if self.srt is None:
                self.srt = SrtWriter()
            return self.srt.path

    def stop_srt(self):
        with self.cond:
            srt, self.srt = self.srt, None
        if srt:
            srt.close()
            return srt.path
        return ""

    # ------------------------------------------------------------ kimenetek
    def state(self):
        with self.cond:
            return self._state()

    def state_json(self):
        return json.dumps(self.state(), ensure_ascii=False)

    def wait_state(self, version, timeout):
        """Megvárja, amíg az állapot változik (az SSE-kapcsolatnak); visszaadja az aktuálisat."""
        with self.cond:
            self.cond.wait_for(lambda: self.version != version, timeout)
            return self._state()

    def history_list(self, n=30):
        with self.cond:
            return list(self.history)[-n:]

    def close(self):
        self.stop_srt()
        if self._log:
            try:
                self._log.close()
            except OSError:
                pass
            self._log = None

    # ------------------------------------------------------------ belső
    def _state(self):
        show_en = self.cfg.show_en_under_hu
        return {"v": self.version,
                "lines": [{"id": ln["id"], "hu": ln["hu"], "en": ln["en"] if show_en else ""} for ln in self.lines],
                "partial": self.partial_text,
                "paused": self.paused,
                "show_en": show_en,
                "style": self.cfg.style}

    def _bump(self):
        """A cond alatt hívandó."""
        self.version += 1
        self.cond.notify_all()
        if self.cfg.write_sub_files:
            self._files_q.put(1)

    def _write_log(self, en, hu, asr_ms, tr_ms, latency_ms):
        if not self.cfg.log_enabled or not self.log_path or self._log_failed:
            return
        try:
            if self._log is None:
                new = not os.path.exists(self.log_path)
                self._log = open(self.log_path, "a", encoding="utf-8", buffering=1)
                if new:
                    self._log.write("ido\tangol\tmagyar\tasr_ms\tforditas_ms\tkeses_ms\n")
            row = [time.strftime("%H:%M:%S"), en, hu, f"{asr_ms:.0f}", f"{tr_ms:.0f}", f"{latency_ms:.0f}"]
            self._log.write("\t".join(x.replace("\t", " ") for x in row) + "\n")
        except OSError as e:
            self._log_failed = True
            self.echo(f"[figyelem] A naplót nem tudom írni ({self.log_path}): {e}")

    def _ticker(self):
        while True:
            write = False
            try:
                self._files_q.get(timeout=0.1)
                write = True
                while True:              # a felgyűlt jelzések egy írásnak számítanak
                    self._files_q.get_nowait()
            except queue.Empty:
                pass
            now = time.monotonic()
            with self.cond:
                changed = False
                if any(ln["until"] < now for ln in self.lines):
                    self.lines = [ln for ln in self.lines if ln["until"] >= now]
                    changed = True
                if self.partial_text and now > self.partial_until:
                    self.partial_text = ""
                    changed = True
                if changed:
                    self._bump()
                txt = "\n".join(ln["hu"] for ln in self.lines)
                js = json.dumps(self._state(), ensure_ascii=False)
            # a fájlírás a zár nélkül történik: nem lassítja a feliratot és az OBS-nek adott választ
            if (write or changed) and self.cfg.write_sub_files and (txt, js) != self._files_written:
                self._files_written = (txt, js)
                atomic_write(SUB_TXT_PATH, txt)
                atomic_write(SUB_JSON_PATH, js)
