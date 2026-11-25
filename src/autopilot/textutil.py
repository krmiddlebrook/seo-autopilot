"""Small text helpers shared across steps: slugs, tokens, similarity, word counts, JSON extraction."""

from __future__ import annotations

import json
import re
import unicodedata
from typing import Any

STOPWORDS = set(
    """a an and are as at be but by can do does for from how i if in into is it its of on or our so than that
    the their them then there these they this to was we what when where which who why will with you your
    vs versus best top guide 2024 2025 2026""".split()
)


def slugify(text: str, max_len: int = 60) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-zA-Z0-9\s-]", "", text).strip().lower()
    slug = re.sub(r"[\s_-]+", "-", text).strip("-")
    if len(slug) > max_len:
        slug = slug[:max_len].rsplit("-", 1)[0]
    return slug


def tokens(text: str, drop_stopwords: bool = True) -> list[str]:
    words = re.findall(r"[a-z0-9]+", text.lower())
    out = []
    for w in words:
        if drop_stopwords and (w in STOPWORDS or len(w) < 2):
            continue
        # crude plural folding keeps "plumbers" and "plumber" together
        if len(w) > 4 and w.endswith("ies"):
            w = w[:-3] + "y"
        elif len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        out.append(w)
    return out


def jaccard(a: str, b: str) -> float:
    ta, tb = set(tokens(a)), set(tokens(b))
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def word_count(text: str) -> int:
    return len(re.findall(r"\b[\w'-]+\b", re.sub(r"<[^>]+>", " ", text)))


def strip_markdown(md: str) -> str:
    md = re.sub(r"```.*?```", " ", md, flags=re.S)
    md = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", md)
    md = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", md)
    md = re.sub(r"^#+\s*", "", md, flags=re.M)
    return re.sub(r"[*_`>]", "", md)


def extract_json(text: str) -> Any:
    """Parse the first JSON object/array in an LLM response (tolerates code fences and chatter)."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, flags=re.S)
    if fenced:
        text = fenced.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    starts = [i for i in (text.find("{"), text.find("[")) if i >= 0]
    if not starts:
        raise ValueError(f"No JSON found in model output: {text[:200]!r}")
    start = min(starts)
    decoder = json.JSONDecoder()
    obj, _ = decoder.raw_decode(text[start:])
    return obj


def clean_model_text(text: str) -> str:
    """Drop reasoning blocks and wrapping code fences from free-text model output."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
    m = re.fullmatch(r"```(?:markdown|md)?\s*(.*?)```", text, flags=re.S)
    return (m.group(1) if m else text).strip()


def truncate_words(text: str, n: int) -> str:
    words = text.split()
    return " ".join(words[:n])


def clamp_text(text: str, max_len: int) -> str:
    text = " ".join(text.split())
    if len(text) <= max_len:
        return text
    cut = text[: max_len - 1].rsplit(" ", 1)[0].rstrip(",.;:-")
    return cut + "…"
