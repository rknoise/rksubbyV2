# -*- coding: utf-8 -*-
"""A GPU, hangkártya és hálózat nélkül tesztelhető részek. Futtatás: python -m unittest discover -s tests"""

import json
import os
import queue
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
_TMP_HOME = tempfile.mkdtemp(prefix="rksubby-test-")
os.environ["RKSUBBY_HOME"] = _TMP_HOME       # a valódi beállításokhoz nem nyúlunk

from subby import config as cfgmod  # noqa: E402
from subby.capture import parse_dshow_list  # noqa: E402
from subby.config import Config  # noqa: E402
from subby.display import SubtitleSink, srt_time  # noqa: E402
from subby.server import SubtitleServer  # noqa: E402
from subby.textutil import (extract_deepl_key, is_sentence_end, looks_like_hallucination,  # noqa: E402
                            mask_key, strip_overlap)
from subby.translate import Translator, llm_system_prompt  # noqa: E402


def make_cfg(**changes):
    c = Config(path=os.path.join(_TMP_HOME, "config.json"), load=False)
    c.update(dict({"log_enabled": False}, **changes))
    return c


class TextTests(unittest.TestCase):
    def test_sentence_end(self):
        self.assertTrue(is_sentence_end("go."))
        self.assertTrue(is_sentence_end("now?"))
        self.assertFalse(is_sentence_end("Mr."))
        self.assertFalse(is_sentence_end("I..."))
        self.assertFalse(is_sentence_end("well,"))

    def test_strip_overlap(self):
        self.assertEqual(strip_overlap("get in the car", "the car now"), "now")
        self.assertEqual(strip_overlap("one two", "three four"), "three four")
        # biztos mondathatáron egyetlen egyező szó valódi ismétlés lehet
        self.assertEqual(strip_overlap("I said go", "go away", min_words=2), "go away")

    def test_hallucination(self):
        class R:
            def __init__(self, text, lp=-0.2, ns=0.0, cr=1.2):
                self.text, self.avg_logprob, self.no_speech_prob, self.compression_ratio = text, lp, ns, cr

        self.assertTrue(looks_like_hallucination(R("Thank you.", ns=0.5)))
        self.assertTrue(looks_like_hallucination(R("Subtitles by the Amara.org community")))
        self.assertTrue(looks_like_hallucination(R("la la la la", cr=3.0)))
        self.assertFalse(looks_like_hallucination(R("Get in the car.")))

    def test_deepl_key(self):
        k = "12345678-abcd-abcd-abcd-1234567890ab:fx"
        self.assertEqual(extract_deepl_key(f"kulcs: {k} {k}"), k)
        self.assertEqual(extract_deepl_key("semmi", strict=True), "")
        self.assertEqual(mask_key(k), "1234******b:fx")


class ConfigTests(unittest.TestCase):
    def test_update_coerces_and_validates(self):
        c = make_cfg()
        changed = c.update({"end_silence_ms": "500", "translator": "nincs-ilyen", "unknown": 1,
                            "style": {"font_size": 500, "color": "piros", "box": "true"}, "max_lines": 9})
        self.assertIn("end_silence_ms", changed)
        self.assertEqual(c.end_silence_ms, 500)
        self.assertEqual(c.translator, "deepl")          # ismeretlen érték: marad a régi
        self.assertEqual(c.style["font_size"], 120)      # korlát
        self.assertEqual(c.style["color"], "#ffff00")    # érvénytelen szín: marad
        self.assertTrue(c.style["box"])
        self.assertEqual(c.max_lines, 4)

    def test_save_load_and_override(self):
        path = os.path.join(_TMP_HOME, "cfg-roundtrip.json")
        c = Config(path=path, load=False)
        c.update({"audio_kind": "dshow", "dshow_device": "AVerMedia HD Capture GC573 1"})
        c.set_override("model", "large-v3-turbo")
        c.save()
        d = Config(path=path)
        self.assertEqual(d.audio_kind, "dshow")
        self.assertEqual(d.dshow_device, "AVerMedia HD Capture GC573 1")
        self.assertEqual(d.model, "distil-large-v3")     # a felülírás nem kerül a fájlba
        with open(path, encoding="utf-8") as f:
            self.assertNotIn("large-v3-turbo", f.read())

    def test_profiles(self):
        ids = [p["id"] for p in cfgmod.list_profiles()]
        self.assertIn("gta5", ids)
        self.assertEqual(ids[0], "altalanos")
        p = cfgmod.save_profile("gta5", {"name": "GTA V saját", "hotwords": "Franklin",
                                         "glossary": [["cops", "zsaruk"], ["", "x"], ["cops", "rendőrök"]]})
        self.assertEqual(p["glossary"], [["cops", "zsaruk"]])
        self.assertTrue(any(x["id"] == "gta5" and x["customized"] for x in cfgmod.list_profiles()))
        cfgmod.reset_profile("gta5")
        self.assertEqual(cfgmod.load_profile("gta5")["name"], "Grand Theft Auto V")
        self.assertEqual(cfgmod.profile_id_from_name("Ő Játékom 2!"), "o-jatekom-2")
        self.assertEqual(cfgmod.load_profile("../rossz")["id"], "altalanos")


class DshowListTests(unittest.TestCase):
    def test_new_format(self):
        text = '''[dshow @ 0] "Iriun Webcam" (video)
[dshow @ 0]   Alternative name "@device_pnp_\\\\?\\root"
[dshow @ 0] "AVerMedia HD Capture GC573 1" (audio, video)
[dshow @ 0] "OBS Virtual Camera" (none)
[dshow @ 0] "CABLE Output (VB-Audio Virtual Cable)" (audio)'''
        devs = {d["name"]: d["types"] for d in parse_dshow_list(text)}
        self.assertEqual(devs["AVerMedia HD Capture GC573 1"], {"audio", "video"})
        self.assertEqual(devs["OBS Virtual Camera"], set())
        self.assertEqual(devs["CABLE Output (VB-Audio Virtual Cable)"], {"audio"})
        self.assertEqual(len(devs), 4)

    def test_old_format(self):
        text = '''[dshow @ 0] DirectShow video devices (some may be both video and audio devices)
[dshow @ 0]  "Cam Link 4K"
[dshow @ 0] DirectShow audio devices
[dshow @ 0]  "Digital Audio Interface (Cam Link 4K)"'''
        devs = {d["name"]: d["types"] for d in parse_dshow_list(text)}
        self.assertEqual(devs["Cam Link 4K"], {"video"})
        self.assertEqual(devs["Digital Audio Interface (Cam Link 4K)"], {"audio"})


class SinkTests(unittest.TestCase):
    def test_final_trim_pause_and_state(self):
        sink = SubtitleSink(make_cfg(max_lines=2), log_path="", echo=lambda *a: None)
        v0 = sink.state()["v"]
        for i in range(3):
            sink.final(f"hu{i}", f"en{i}", 100, 200, 600)
        st = sink.state()
        self.assertGreater(st["v"], v0)
        self.assertEqual([ln["hu"] for ln in st["lines"]], ["hu1", "hu2"])
        self.assertEqual(st["lines"][0]["en"], "")        # angol csak, ha be van kapcsolva
        self.assertEqual(len(sink.history_list()), 3)
        sink.set_paused(True)
        sink.final("rejtett", "hidden", 1, 1, 1)
        self.assertEqual(sink.state()["lines"], [])
        self.assertEqual(sink.history_list()[-1]["hu"], "rejtett")

    def test_srt_time(self):
        self.assertEqual(srt_time(0), "00:00:00,000")
        self.assertEqual(srt_time(3725.5), "01:02:05,500")


class TranslatorTests(unittest.TestCase):
    def test_cache_fallback_and_stats(self):
        calls = []

        def bad(text, ctx):
            calls.append("bad")
            raise RuntimeError("nem megy")

        def good(text, ctx):
            calls.append("good")
            return "HU " + text

        tr = Translator([("rossz", bad), ("jo", good)], log=lambda *a: None)
        hu, _ms = tr.submit("Hello there.", [], kind="spec").result(5)
        self.assertEqual(hu, "HU Hello there.")
        hu2, _ = tr.submit("Hello there.", [], kind="final").result(5)   # a gyorsítótárból
        self.assertEqual(hu2, hu)
        self.assertEqual(calls, ["bad", "good"])
        tr.submit("Another one.", [], kind="final").result(5)            # a rossz most szünetel
        self.assertEqual(calls[-1], "good")
        st = tr.snapshot()
        self.assertEqual(st["spec_hits"], 1)
        self.assertEqual(st["sent_chars"], len("Hello there.") + len("Another one."))
        tr.note_shown("Hello there.")
        self.assertGreater(tr.snapshot()["waste_pct"], 0)
        tr.shutdown()

    def test_llm_prompt(self):
        s = llm_system_prompt("Grand Theft Auto VI", [["cops", "zsaruk"]], "prefer_less")
        self.assertIn("Grand Theft Auto VI", s)
        self.assertIn("cops = zsaruk", s)
        self.assertIn("tegezés", s)


class PartialFixTests(unittest.TestCase):
    """Kikapcsolt angol előnézetnél rövid beszéd közben ne fusson fölösleges felismerés."""

    def test_no_partial_asr_when_preview_off(self):
        try:
            import numpy as np  # noqa: F401

            from subby.subtitler import LiveSubtitler, Utterance
        except ImportError:
            self.skipTest("numpy nincs telepítve")
        import numpy as np

        class FakeAsr:
            calls = 0

            def transcribe(self, *a, **k):
                FakeAsr.calls += 1
                raise AssertionError("nem kellett volna felismerni")

        cfg = make_cfg(show_partial_en=False)
        sink = SubtitleSink(cfg, log_path="", echo=lambda *a: None)
        sub = LiveSubtitler(cfg, FakeAsr(), lambda f: 0.9, None, sink, queue.Queue(), log=lambda *a: None)
        sub.utt = Utterance(0.5, 0.35)
        for _ in range(40):                    # ~1,3 mp beszéd: rövidebb, mint a korai lezárás határa
            sub.utt.append(np.zeros(512, np.float32), 0.9, time.monotonic())
        sub.last_partial_t = 0.0
        sub._maybe_partial()
        self.assertEqual(FakeAsr.calls, 0)


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import socket

        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        cls.port = s.getsockname()[1]
        s.close()

        class FakeApi:
            def get(self, name, q):
                return 200, {"name": name}

            def post(self, name, body):
                return 200, {"name": name, "body": body}

        cls.sink = SubtitleSink(make_cfg(), log_path="", echo=lambda *a: None)
        cls.server = SubtitleServer(cls.sink, cls.port, FakeApi())
        assert cls.server.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()

    def req(self, path, data=None, headers=None):
        r = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, headers=headers or {})
        try:
            with urllib.request.urlopen(r, timeout=5) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def test_pages_and_state(self):
        code, body = self.req("/")
        self.assertEqual(code, 200)
        self.assertIn(b"subs.js", body)
        for path in ("/app", "/player", "/static/app.js", "/static/player.js", "/static/subs.js"):
            self.assertEqual(self.req(path)[0], 200, path)
        self.assertEqual(self.req("/static/../config.py")[0], 404)
        code, body = self.req("/state")
        self.assertIn("lines", json.loads(body))
        self.assertEqual(json.loads(self.req("/api/ping")[1])["app"], "rksubby")

    def test_api_protection(self):
        ok = {"Content-Type": "application/json"}
        self.assertEqual(self.req("/api/x", b"{}", ok)[0], 200)
        self.assertEqual(self.req("/api/x", b"{}", {"Content-Type": "text/plain"})[0], 415)
        evil = dict(ok, Origin="http://gonosz.example")
        self.assertEqual(self.req("/api/x", b"{}", evil)[0], 403)
        self.assertEqual(self.req("/state", headers={"Host": "gonosz.example"})[0], 403)

    def test_sse(self):
        got = []

        def listen():
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/events", timeout=5) as r:
                for raw in r:
                    line = raw.decode("utf-8").strip()
                    if line.startswith("data: {"):
                        got.append(json.loads(line[6:]))
                        if len(got) == 2:
                            return

        t = threading.Thread(target=listen, daemon=True)
        t.start()
        time.sleep(0.3)
        self.sink.final("Szia!", "Hi!", 1, 1, 1)
        t.join(5)
        self.assertEqual(len(got), 2)
        self.assertEqual(got[1]["lines"][-1]["hu"], "Szia!")


if __name__ == "__main__":
    unittest.main()
