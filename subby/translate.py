# -*- coding: utf-8 -*-
"""Fordítás: DeepL (glosszáriummal), helyi Ollama, Google; aszinkron, gyorsítótárral, tartalékkal
és karakterstatisztikával (mennyi DeepL-keret megy el az előre fordításra)."""

import collections
import concurrent.futures
import hashlib
import threading
import time

from .textutil import clean_spaces, short_err

_TG_PROMPT = (
    "You are a professional English (en) to Hungarian (hu) translator. Your goal is to accurately "
    "convey the meaning and nuances of the original English text while adhering to Hungarian grammar, "
    "vocabulary, and cultural sensitivities.\n"
    "Produce only the Hungarian translation, without any additional explanations or commentary. "
    "Please translate the following English text into Hungarian:\n\n\n{text}"
)


def llm_system_prompt(game_hint="", glossary=None, formality="prefer_less"):
    what = f"dialogue from the video game {game_hint}" if game_hint else "video game and video dialogue"
    s = (f"You translate English {what} into natural, colloquial Hungarian subtitles. Keep the meaning, "
         "tone and slang; swearing is fine. Keep names unchanged. ")
    if formality == "prefer_less":
        s += "Characters address each other informally (tegezés). "
    elif formality == "prefer_more":
        s += "Use the formal form of address (magázás). "
    if glossary:
        s += "Always use these translations: " + "; ".join(f"{en} = {hu}" for en, hu in glossary) + ". "
    return s + ("Use the previous lines only as context. Output ONLY the Hungarian translation of the line "
                "to translate, with no quotes or explanations.")


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


# -----------------------------------------------------------------------------
# Ollama
# -----------------------------------------------------------------------------

def ollama_status(url, model):
    """(fut-e az Ollama, használható modell neve vagy None)"""
    try:
        import requests

        r = requests.get(url.rstrip("/") + "/api/tags", timeout=0.7)
        r.raise_for_status()
        names = [m.get("name", "") for m in r.json().get("models", [])]
    except Exception:
        return False, None
    want = model if ":" in model else model + ":latest"
    if want in names:
        return True, want
    for pref in ("translategemma:12b", "translategemma:27b", "translategemma:4b", "translategemma:latest"):
        if pref in names:
            return True, pref
    return True, None


# -----------------------------------------------------------------------------
# DeepL
# -----------------------------------------------------------------------------

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


def _deepl_headers(key):
    return {"Authorization": f"DeepL-Auth-Key {key}"}


def deepl_usage(key, timeout=8.0):
    """(felhasznált karakterek, havi keret) az aktuális időszakban."""
    import requests

    r = requests.get(deepl_host(key) + "/v2/usage", headers=_deepl_headers(key), timeout=timeout)
    _deepl_check(r)
    d = r.json()
    return int(d.get("character_count", 0)), int(d.get("character_limit", 0))


def glossary_tsv(entries):
    return "\n".join(f"{en}\t{hu}" for en, hu in entries)


def ensure_deepl_glossary(key, profile_id, entries, timeout=8.0):
    """A profil glosszáriuma a DeepL-nél (létrehozza, ha még nincs; a régi változatot törli).
    Visszaad: (glossary_id vagy None, üzenet)."""
    if not entries:
        return None, "nincs glosszárium a profilban"
    import requests

    host, headers = deepl_host(key), _deepl_headers(key)
    r = requests.get(host + "/v2/glossary-language-pairs", headers=headers, timeout=timeout)
    _deepl_check(r)
    pairs = {(p.get("source_lang", "").lower(), p.get("target_lang", "").lower())
             for p in r.json().get("supported_languages", [])}
    if ("en", "hu") not in pairs:
        return None, "a DeepL glosszárium az angol→magyar párosnál nem érhető el; a nevek így is megmaradnak"
    tsv = glossary_tsv(entries)
    prefix = f"rksubby-{profile_id}-"
    name = prefix + hashlib.sha1(tsv.encode("utf-8")).hexdigest()[:10]
    r = requests.get(host + "/v2/glossaries", headers=headers, timeout=timeout)
    _deepl_check(r)
    existing = r.json().get("glossaries", [])
    for g in existing:
        if g.get("name") == name and g.get("ready", True):
            return g["glossary_id"], f"glosszárium: {len(entries)} kifejezés"
    r = requests.post(host + "/v2/glossaries", headers=headers, timeout=timeout,
                      json={"name": name, "source_lang": "en", "target_lang": "hu",
                            "entries": tsv, "entries_format": "tsv"})
    _deepl_check(r)
    gid = r.json()["glossary_id"]
    for g in existing:   # a profil korábbi glosszáriumai már nem kellenek
        if g.get("name", "").startswith(prefix) and g.get("glossary_id") != gid:
            try:
                requests.delete(f"{host}/v2/glossaries/{g['glossary_id']}", headers=headers, timeout=timeout)
            except Exception:
                pass
    return gid, f"glosszárium létrehozva: {len(entries)} kifejezés"


# -----------------------------------------------------------------------------
# Fordítók
# -----------------------------------------------------------------------------

def make_backend(kind, cfg, profile=None, deepl_key="", ollama_model=None, glossary_id=None):
    """Egy fordító: fn(text, context_list) -> magyar szöveg."""
    kind = kind.lower()
    profile = profile or {}
    timeout_s = cfg.translate_timeout_s
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
                               timeout=timeout_s)
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
        if not deepl_key:
            raise RuntimeError("nincs DeepL-kulcs")
        host = deepl_host(deepl_key)
        sess = _ThreadLocalSession(_deepl_headers(deepl_key))

        def deepl(text, ctx):
            body = {"text": [text], "source_lang": "EN", "target_lang": "HU"}
            if cfg.deepl_formality:               # menet közben is átállítható
                body["formality"] = cfg.deepl_formality
            if glossary_id:
                body["glossary_id"] = glossary_id
            if ctx:
                body["context"] = " ".join(ctx)   # nem fordítja le és nem számolja bele a keretbe
            r = sess.get().post(host + "/v2/translate", json=body, timeout=timeout_s)
            _deepl_check(r)
            return r.json()["translations"][0]["text"]

        return deepl

    if kind == "ollama":
        model = ollama_model or cfg.ollama_model
        sess = _ThreadLocalSession()
        url = cfg.ollama_url.rstrip("/")
        is_tg = "translategemma" in model.lower()
        timeout = max(timeout_s, 30.0)   # az első hívás betölti a modellt a GPU-ra

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
            system = llm_system_prompt(profile.get("llm_hint", ""), profile.get("glossary"), cfg.deepl_formality)
            body = {"model": model, "stream": False, "keep_alive": "30m",
                    "options": {"temperature": 0.2},
                    "messages": [{"role": "system", "content": system},
                                 {"role": "user", "content": user}]}
            r = sess.get().post(url + "/api/chat", json=body, timeout=timeout)
            r.raise_for_status()
            return r.json()["message"]["content"].strip().strip('"')

        return ollama

    raise RuntimeError(f"ismeretlen fordító: {kind}")


AUTO_CHAIN = ["deepl", "ollama"]   # tartalék sorrend; a Google csak kifejezett kérésre


def build_chain(cfg, profile=None, deepl_key="", glossary_id=None, log=print):
    """A választott fordító elöl, mögötte tartaléknak a többi elérhető (ha az első nem válaszol)."""
    kind = cfg.translator.lower()
    if kind == "none":
        return []
    order = AUTO_CHAIN if kind == "auto" else [kind] + [k for k in AUTO_CHAIN if k != kind]
    chain = []
    for k in order:
        try:
            if k == "deepl" and not deepl_key:
                if kind in ("deepl", "auto"):
                    log("[fordító] NINCS DeepL-kulcs (a beállító ablak Fordítás lépésénél adhatod meg)")
                continue
            if k == "ollama":
                running, model = ollama_status(cfg.ollama_url, cfg.ollama_model)
                if not model:
                    if kind == "ollama":
                        log("[fordító] Ollama: " + ("fut, de nincs letöltve fordítómodell: ollama pull "
                                                    "translategemma:12b" if running else "nem fut."))
                    continue
                chain.append((f"ollama ({model})", make_backend("ollama", cfg, profile, ollama_model=model)))
                continue
            chain.append((k, make_backend(k, cfg, profile, deepl_key, glossary_id=glossary_id)))
        except Exception as e:
            log(f"[fordító] {k} nem használható: {short_err(e)}")
    return chain


def test_one(kind, cfg, profile=None, deepl_key="", text="Get in the car, Franklin, before the cops get here!"):
    """Egy fordító kipróbálása: {"ok", "name", "ms", "text" | "error", ["usage"]}."""
    name = kind
    try:
        if kind == "deepl":
            if not deepl_key:
                return {"ok": False, "name": name, "error": "nincs DeepL-kulcs"}
            used, limit = deepl_usage(deepl_key)
            usage = {"used": used, "limit": limit}
        else:
            usage = None
        if kind == "ollama":
            running, model = ollama_status(cfg.ollama_url, cfg.ollama_model)
            if not model:
                return {"ok": False, "name": name,
                        "error": ("fut, de nincs fordítómodell: ollama pull translategemma:12b"
                                  if running else "az Ollama nem fut (vagy nincs telepítve)")}
            fn, name = make_backend("ollama", cfg, profile, ollama_model=model), f"ollama ({model})"
        else:
            fn = make_backend(kind, cfg, profile, deepl_key)
        t0 = time.perf_counter()
        out = fn(text, [])
        res = {"ok": bool(out and out.strip()), "name": name, "ms": round((time.perf_counter() - t0) * 1000),
               "text": clean_spaces(out or "")}
        if usage:
            res["usage"] = usage
        return res
    except Exception as e:
        return {"ok": False, "name": name, "error": short_err(e)}


class Translator:
    """Aszinkron fordítás gyorsítótárral (ugyanazt a mondatot csak egyszer fordítja le) és
    tartalékkal: ha egy szolgáltatás hibázik, egy ideig a következőt használja."""

    def __init__(self, chain, log=print, workers=4):
        self.chain = chain
        self.log = log
        self.down_until = {name: 0.0 for name, _ in self.chain}
        self.backoff = {name: 30.0 for name, _ in self.chain}
        self.last_used = None
        # 4 szál: egy végleges fordítás ne várjon az előre indított (spekulatív) kérések mögött
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=workers, thread_name_prefix="fordito")
        self.cache = collections.OrderedDict()
        self.lock = threading.Lock()
        self.stats = {"requests": 0, "spec_requests": 0, "sent_chars": 0, "shown_chars": 0,
                      "spec_hits": 0, "failures": 0, "by_backend": {}}

    def names(self):
        return " -> ".join(name for name, _ in self.chain) or "nincs (angol szöveg jelenik meg)"

    def probe(self):
        """Indításkori próba: kiderül, melyik fordító működik, és felépül a kapcsolat."""
        if not self.chain:
            return "kikapcsolva, az angol szöveg jelenik meg"
        try:
            hu, ms = self.submit("Get in the car.", [], kind="probe").result(timeout=90)
        except Exception:
            hu, ms = None, 0
        if hu:
            return f"{self.last_used} működik ('Get in the car.' -> '{hu}', {ms:.0f} ms)"
        return "EGYIK FORDÍTÓ SEM VÁLASZOLT, az angol szöveg jelenik meg"

    def submit(self, text, context, kind="final"):
        key = text.strip()
        with self.lock:
            fut = self.cache.get(key)
            if fut is not None:
                self.cache.move_to_end(key)
                if kind == "final" and getattr(fut, "rk_kind", "") == "spec":
                    self.stats["spec_hits"] += 1   # az előre indított fordítás bevált
                    fut.rk_kind = "final"
                return fut
            fut = self.pool.submit(self._run, key, tuple(context), kind)
            fut.rk_kind = kind
            self.cache[key] = fut
            while len(self.cache) > 200:
                self.cache.popitem(last=False)
            return fut

    def note_shown(self, en):
        """A kijelző jelzi, hogy egy lefordított mondat ténylegesen kikerült."""
        with self.lock:
            self.stats["shown_chars"] += len(en)

    def snapshot(self):
        with self.lock:
            s = dict(self.stats, by_backend=dict(self.stats["by_backend"]))
        sent, shown = s["sent_chars"], s["shown_chars"]
        s["waste_pct"] = round(max(0.0, 1 - shown / sent) * 100) if sent else 0
        return s

    def _run(self, text, context, kind):
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
                    if kind != "probe":
                        self.stats["requests"] += 1
                        self.stats["spec_requests"] += kind == "spec"
                        self.stats["sent_chars"] += len(text)
                        self.stats["by_backend"][name] = self.stats["by_backend"].get(name, 0) + len(text)
                self.last_used = name
                return clean_spaces(out), (time.perf_counter() - t0) * 1000
            except Exception as e:
                with self.lock:
                    wait = self.backoff[name]
                    self.down_until[name] = time.monotonic() + wait
                    self.backoff[name] = min(300.0, wait * 2)
                    self.stats["failures"] += 1
                rest = "a tartalék fordító megy" if len(self.chain) > 1 else "angol felirat jelenik meg"
                self.log(f"[fordító] {name}: {short_err(e)} -> {wait:.0f} mp szünet, addig {rest}")
        with self.lock:
            self.cache.pop(text, None)   # legközelebb újrapróbálja
        return None, (time.perf_counter() - t0) * 1000

    def shutdown(self):
        self.pool.shutdown(wait=False, cancel_futures=True)
