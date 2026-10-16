# -*- coding: utf-8 -*-
"""Élő feliratozó: VAD-alapú darabolás, élő előnézet, korai lezárás, spekulatív felismerés+fordítás."""

import collections
import queue
import time
from dataclasses import dataclass

import numpy as np

from .textutil import clean_spaces, is_sentence_end, looks_like_hallucination, norm_text, norm_word, strip_overlap
from .vad import FRAME, FRAME_S, ms2frames


class Utterance:
    """Egy folyamatban lévő beszédszakasz: 32 ms-os ablakok + VAD-valószínűségek."""

    def __init__(self, on, off, continuation=False):
        self.on, self.off = on, off  # beszéd- és csendküszöb
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
        if p >= self.on:
            self.speech_frames += 1
            self.silence_run = 0
            self.last_speech = i
        elif p < self.off or self.silence_run > 0:
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
        u = Utterance(self.on, self.off, continuation=True)
        for f, p, t in zip(self.frames[idx:], self.probs[idx:], self.times[idx:]):
            u.append(f, p, t)
        return u


@dataclass
class FinalItem:
    en: str
    future: object
    t_speech_end: float
    asr_ms: float


def _db(power):
    return float(10 * np.log10(max(power, 1e-12)))


class LiveSubtitler:
    def __init__(self, cfg, asr, vad, translator, sink, display_q, log=print, debug=False):
        c = cfg
        self.cfg = cfg
        self.asr, self.vad, self.translator = asr, vad, translator
        self.sink, self.display_q = sink, display_q
        self.log, self.debug = log, debug
        self.vad_on, self.vad_off = c.vad_threshold, c.vad_neg_threshold
        self.start_frames = ms2frames(c.start_speech_ms)
        self.min_utt_frames = ms2frames(c.min_utterance_speech_ms)
        self.end_frames = ms2frames(c.end_silence_ms)
        self.spec_frames = (ms2frames(c.speculative_silence_ms)
                            if 0 < c.speculative_silence_ms < c.end_silence_ms else 0)
        self.post_frames = ms2frames(c.post_roll_ms)
        self.max_utt_s = c.max_utterance_s
        self.smart_endpoint = c.smart_endpoint
        self.final_beam, self.partial_beam = c.final_beam_size, c.partial_beam_size
        self.partial_interval = c.partial_interval_s
        self.early_commit, self.early_min_s = c.early_commit, c.early_commit_min_s
        self.early_guard_s = c.early_commit_guard_s
        self.use_prompt, self.context_reset_s = c.use_context_prompt, c.context_reset_s
        self.spec_translate = c.spec_translate
        if c.audio_kind == "dshow":
            self.silence_hint = "NEM JÖN HANG (néma a kártya hangja). Be van kapcsolva a konzol, és szól a játék/videó?"
        elif "cable" in (c.device_name or "").lower():
            self.silence_hint = ("NEM JÖN HANG (néma a bemenet). Szól a játék/videó? Az OBS-ben a játék hangja "
                                 "tényleg a CABLE Inputra megy?")
        else:
            self.silence_hint = "NEM JÖN HANG (néma a bemenet). Szól a játék/videó? Jó bemenetet választottál?"
        self.preroll = collections.deque(maxlen=ms2frames(c.pre_roll_ms) + self.start_frames)
        self.start_run = 0
        self.utt = None
        self.prefinal = None         # (last_speech index, szöveg, asr_ms)
        self.prev_words = []         # az előző előnézet szavai (egyezésvizsgálathoz)
        self.last_partial_t = 0.0
        self.context = collections.deque(maxlen=max(1, c.context_lines))
        self.last_final_t = 0.0
        self.last_committed = ""
        self.cut_kind = None
        self.lag = 0.0
        self.last_audio_t = time.monotonic()
        # állapotkijelzés (hangszint), hogy látszódjon, jön-e hang
        self.total_frames = 0
        self.next_status = ms2frames(5000)
        self.st_frames, self.st_sumsq, self.st_peak, self.st_vad, self.st_finals = 0, 0.0, 0.0, 0.0, 0
        self.status_msg = "várom a hangot..."
        # a grafikus felület hangszintmérője (256 ms-onként frissül)
        self.level = {"rms_db": -120.0, "peak_db": -120.0, "speech": 0.0}
        self.lv_frames, self.lv_sumsq, self.lv_peak, self.lv_vad = 0, 0.0, 0.0, 0.0

    def dbg(self, *a):
        if self.debug:
            self.log("[debug] " + " ".join(str(x) for x in a))

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
                self.last_audio_t = time.monotonic()
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
                    self.log(f"[hiba a feldolgozásban] {e!r}")
                    self._end_utterance()
                pos = end
            if pos:
                buf = buf[pos:]
                marks = collections.deque((e - pos, t) for e, t in marks)
            try:
                self._maybe_partial()
            except Exception as e:
                self.log(f"[hiba az előnézetben] {e!r}")

    # ------------------------------------------------------------- egy ablak
    def _on_frame(self, frame, t):
        p = self.vad(frame)
        self.lag = time.monotonic() - t
        self._stats(frame, p)
        if self.utt is None:
            self.preroll.append((frame, p, t))
            self.start_run = self.start_run + 1 if p >= self.vad_on else 0
            if self.start_run >= self.start_frames:
                self._start_utterance()
            return
        u = self.utt
        u.append(frame, p, t)
        if self.spec_frames and u.silence_run == self.spec_frames:
            self._prefinal()
            pf = self.prefinal
            if self.smart_endpoint and pf and pf[1] and is_sentence_end(pf[1].split()[-1]):
                self._finalize_silence()   # mondatvégi írásjel + rövid szünet: nem várunk tovább
                return
        if u.silence_run >= self.end_frames:
            self._finalize_silence()
        elif u.duration() >= self.max_utt_s:
            self._finalize_max()

    def _stats(self, frame, p):
        sq = float(np.dot(frame, frame))
        pk = float(np.max(np.abs(frame)))
        self.total_frames += 1
        self.st_frames += 1
        self.st_sumsq += sq
        self.st_peak = max(self.st_peak, pk)
        self.st_vad = max(self.st_vad, p)
        self.lv_frames += 1
        self.lv_sumsq += sq
        self.lv_peak = max(self.lv_peak, pk)
        self.lv_vad = max(self.lv_vad, p)
        if self.lv_frames >= 8:
            self.level = {"rms_db": round(_db(self.lv_sumsq / (self.lv_frames * FRAME)), 1),
                          "peak_db": round(20 * float(np.log10(max(self.lv_peak, 1e-6))), 1),
                          "speech": round(self.lv_vad, 2)}
            self.lv_frames, self.lv_sumsq, self.lv_peak, self.lv_vad = 0, 0.0, 0.0, 0.0
        if self.total_frames >= self.next_status:
            self._status()

    def _status(self):
        """Ha egy ideje nem volt felirat: kiírja, jön-e hang és hall-e benne beszédet."""
        elapsed = self.total_frames * FRAME_S
        self.next_status = self.total_frames + ms2frames((10 if elapsed < 60 else 60) * 1000)
        rms_db = _db(self.st_sumsq / max(1, self.st_frames * FRAME))
        peak_db = 20 * np.log10(max(self.st_peak, 1e-6))
        if peak_db < -60:
            msg = self.silence_hint
        elif self.st_vad < self.vad_on:
            msg = f"jön hang ({rms_db:.0f} dBFS, csúcs {peak_db:.0f} dBFS), de beszédet nem hallok benne"
        elif self.st_finals == 0:
            msg = f"hallok beszédet ({rms_db:.0f} dBFS), feldolgozom"
        else:
            msg = f"feliratozok ({self.st_finals} mondat az utóbbi időben)"
        self.status_msg = msg
        if self.st_finals == 0:
            lag = f", lemaradás: {self.lag:.1f} s" if self.lag > 1.0 else ""
            self.log(f"[állapot {elapsed:5.0f} s] {msg}{lag}")
        self.st_frames, self.st_sumsq, self.st_peak, self.st_vad, self.st_finals = 0, 0.0, 0.0, 0.0, 0

    def _start_utterance(self):
        if self.last_final_t and time.monotonic() - self.last_final_t > self.context_reset_s:
            self.context.clear()
            self.last_committed = ""
        self.utt = Utterance(self.vad_on, self.vad_off)
        for f, p, t in self.preroll:
            self.utt.append(f, p, t)
        self.preroll.clear()
        self.start_run = 0
        self.prefinal = None
        self.prev_words = []
        self.last_partial_t = time.monotonic()
        self.dbg("beszéd kezdete")

    def _end_utterance(self):
        self.utt = None
        self.prefinal = None
        self.prev_words = []
        self.sink.partial("")

    # ------------------------------------------------------------- felismerés
    def _prompt(self):
        if not self.use_prompt or not self.context:
            return None
        return " ".join(self.context)[-220:]

    def _clean(self, res):
        if looks_like_hallucination(res):
            if res.text:
                self.dbg(f"kiszűrve: {res.text!r} (logprob {res.avg_logprob:.2f}, "
                         f"no_speech {res.no_speech_prob:.2f})")
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
        res = self.asr.transcribe(self._speech_audio(u), beam_size=self.final_beam, prompt=self._prompt())
        text = self._clean(res)
        if u.continuation:
            text = self._strip(text)
        self.prefinal = (u.last_speech, text, res.ms)
        if text:
            self.sink.partial(text)
            if self.spec_translate:
                self.translator.submit(text, list(self.context), kind="spec")
        self.dbg(f"előzetes ({res.ms:.0f} ms): {text!r}")

    def _finalize_silence(self):
        u = self.utt
        dur = (u.last_speech + 1) * FRAME_S
        if u.speech_frames < self.min_utt_frames:
            self.dbg(f"eldobva: túl rövid ({u.speech_frames * FRAME_S * 1000:.0f} ms beszéd)")
            self._end_utterance()
            return
        if self.prefinal and self.prefinal[0] == u.last_speech:
            text, asr_ms = self.prefinal[1], self.prefinal[2]   # az előzetes eredmény még érvényes
        else:
            res = self.asr.transcribe(self._speech_audio(u), beam_size=self.final_beam, prompt=self._prompt())
            text, asr_ms = self._clean(res), res.ms
            if u.continuation:
                text = self._strip(text)
        self.dbg(f"szakasz vége (csend), {dur:.2f} s")
        self._emit_final(text, u.times[u.last_speech], asr_ms)
        self._end_utterance()

    def _finalize_max(self):
        u = self.utt
        n = len(u)
        lo, hi = max(1, n - ms2frames(3000)), max(2, n - ms2frames(200))
        cut = lo + int(np.argmin(u.probs[lo:hi]))
        res = self.asr.transcribe(u.audio(cut + 1), beam_size=self.final_beam, prompt=self._prompt())
        text = self._clean(res)
        if u.continuation:
            text = self._strip(text)
        self.dbg(f"szakasz vágása (max hossz) {(cut + 1) * FRAME_S:.2f} s-nál")
        self._emit_final(text, u.times[cut], res.ms)
        self.utt = u.tail_from(cut + 1)
        self.cut_kind = "max"          # kényszervágás: egy szó átlóghat, az ismétlést ki kell szűrni
        self.prefinal = None
        self.prev_words = []

    def _maybe_partial(self):
        u = self.utt
        if u is None or self.partial_interval <= 0 or u.silence_run > 0:
            return
        now = time.monotonic()
        if now - self.last_partial_t < self.partial_interval or u.speech_frames * FRAME_S < 0.5:
            return
        if self.lag > 0.6:           # le vagyunk maradva: előbb behozzuk a hangot
            return
        want_words = self.early_commit and u.duration() >= self.early_min_s
        show = self.cfg.show_partial_en
        if not want_words and not show:
            # Nincs mire használni az eredményt: ne foglalja a GPU-t, és ne késleltesse a mondatvég
            # észlelését (a felismerés alatt a VAD sem fut).
            return
        self.last_partial_t = now
        res = self.asr.transcribe(u.audio(), beam_size=self.partial_beam, word_timestamps=want_words,
                                  prompt=self._prompt())
        if want_words and res.words and not looks_like_hallucination(res):
            commit = self._find_commit(res.words)
            if commit:
                k, cut = commit
                head = clean_spaces("".join(w.word for w in res.words[:k]))
                rest = clean_spaces("".join(w.word for w in res.words[k:]))
                if u.continuation:
                    head = self._strip(head)
                self.dbg(f"korai lezárás {(cut + 1) * FRAME_S:.2f} s-nál: {head!r}")
                self._emit_final(head, u.times[cut], res.ms)
                self.utt = u.tail_from(cut + 1)
                self.cut_kind = "sentence"   # biztos mondathatár a két szó közötti csendben
                self.prefinal = None
                self.prev_words = [norm_word(w.word) for w in res.words[k:]]
                self.sink.partial(rest)
                return
            cand = self._candidate_end(res.words)
            if cand is not None and self.spec_translate:
                # a mondatvég már látszik: a fordítás előre indul, mire lezárjuk, kész lesz
                head = clean_spaces("".join(w.word for w in res.words[:cand + 1]))
                if u.continuation:
                    head = self._strip(head)
                if norm_text(head):
                    self.translator.submit(head, list(self.context), kind="spec")
            self.prev_words = [norm_word(w.word) for w in res.words]
        else:
            self.prev_words = []
        if show:
            text = self._clean(res)
            if text and u.continuation:
                text = self._strip(text)
            self.sink.partial(text)

    def _candidate_end(self, words):
        """Az utolsó mondatvég, amely után már elég hang van (early_commit_guard_s)."""
        dur = self.utt.duration()
        cand = None
        for i in range(len(words) - 1):
            if is_sentence_end(words[i].word) and words[i].end <= dur - self.early_guard_s:
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
        fut = self.translator.submit(text, list(self.context), kind="final")
        self.context.append(text)
        self.last_committed = text
        self.last_final_t = time.monotonic()
        self.st_finals += 1
        self.display_q.put(FinalItem(text, fut, t_speech_end, asr_ms))
