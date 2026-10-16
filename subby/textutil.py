# -*- coding: utf-8 -*-
"""Szövegsegédek: normalizálás, mondatvég, átfedés-vágás, Whisper-hallucinációk szűrése, kulcskezelés."""

import re

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
    """r: felismerési eredmény (text, avg_logprob, no_speech_prob, compression_ratio)."""
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
# Kulcsok, számok, hibák
# -----------------------------------------------------------------------------

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


def fmt_int(n):
    return f"{n:,}".replace(",", " ")


def short_err(e):
    s = str(e).strip()
    s = s.splitlines()[0] if s else type(e).__name__
    return s if len(s) <= 140 else s[:140] + "..."
