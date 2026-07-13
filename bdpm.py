# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Shared BDPM catalog helpers: tokenising drug names, and scoring/ordering CIS
against a frequency list.

These functions are pure-stdlib and path-agnostic (every input file is passed in
explicitly), so both the freshness scraper (``scrape-rcp.py``) and the static
builder (``build.py``) import them instead of each carrying a copy. scrape-rcp.py
uses them to order its scrape queue by prescription frequency; build.py reuses the
very same tokenising + frequency scoring to pick the single canonical target page
for each cross-drug backlink (see ``build_xref_index``). Keeping them here is what
lets build.py stay lean (lxml + brotli, no httpx/loguru/click) while still sharing
the exact scoring, rather than duplicating it.

This module is import-safe (no side effects at import) and has no third-party
dependencies, so importing it from either script's environment always works.
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
import unicodedata


def tokens(text: str) -> set[str]:
    """Normalise a drug name/term to a set of comparable word tokens.

    Uppercases, strips accents (paracétamol -> PARACETAMOL) and splits on any
    non-alphanumeric run, so a term matches a denomination regardless of case,
    accents, dosage punctuation or word order.
    """
    folded = unicodedata.normalize("NFKD", text.upper())
    folded = folded.encode("ascii", "ignore").decode("ascii")
    return {tok for tok in re.split(r"[^A-Z0-9]+", folded) if tok}


def read_catalog(path: Path) -> list[tuple[str, str]]:
    """Return ``(cis, denomination)`` pairs from ``CIS_bdpm.txt`` in file order.

    The official BDPM file is latin-1, tab-separated: column 0 is the 8-digit
    CIS, column 1 the drug name (same source build.load_names reads for names).
    File order is preserved and used as the stable tiebreak for equal scores.
    """
    catalog: list[tuple[str, str]] = []
    with path.open(encoding="latin-1") as fh:
        for line in fh:
            parts = line.split("\t")
            cis = parts[0].strip()
            if cis:
                name = parts[1].strip() if len(parts) > 1 else ""
                catalog.append((cis, name))
    return catalog


def column_tokens(
    path: Path, cis_col: int, text_col: int, keep: set[str] | None = None
) -> dict[str, set[str]]:
    """Map ``CIS -> union of tokens from text_col`` across a latin-1 TSV export.

    Rows whose CIS is not in ``keep`` (when given) are skipped, so foreign CIS
    never leak in; a missing file yields an empty mapping. Shared by
    ``substance_signals`` (folding the COMPO/GENER columns into a CIS's token
    pool) and by build.py's backlink index (reading the active-substance tokens
    per CIS to seed link terms).
    """
    out: dict[str, set[str]] = {}
    if not path.is_file():  # missing, or a stray dir from a bad single-file mount
        return out
    with path.open(encoding="latin-1") as fh:
        for line in fh:
            parts = line.split("\t")
            if len(parts) <= max(cis_col, text_col):
                continue
            cis = parts[cis_col].strip()
            if keep is not None and cis not in keep:
                continue
            out.setdefault(cis, set()).update(tokens(parts[text_col]))
    return out


def substance_signals(
    catalog: list[tuple[str, str]],
    compo_path: Path | None = None,
    gener_path: Path | None = None,
) -> dict[str, set[str]]:
    """Build, per CIS, the token pool used to match it against the frequency list.

    Commercial names alone match only ~73% of drugs, because many brands hide
    their active substance (XENAZINE, REMINYL, ENANTYUM ...). Two extra BDPM
    exports recover a large slice of the rest by adding, to each CIS's name
    tokens:

    * ``CIS_COMPO_bdpm`` - the active-substance denomination(s) (column 3), so a
      brand matches a *substance* frequency term (e.g. a REMINYL generic ->
      GALANTAMINE).
    * ``CIS_GENER_bdpm`` - the generic-group label, which reads
      ``"<substance> <dose> - <reference brand> <dose>, <form>"``; folding it in
      lets a generic also match its *reference brand* term.

    Both files are optional. When neither path is given (or the file is absent)
    this returns exactly the name tokens, so matching degrades gracefully to the
    previous behaviour. Only CIS already in ``catalog`` are populated (foreign
    CIS are ignored).
    """
    signals: dict[str, set[str]] = {cis: tokens(name) for cis, name in catalog}
    keep = set(signals)
    if compo_path is not None:
        for cis, toks in column_tokens(compo_path, 0, 3, keep).items():
            signals[cis] |= toks  # CIS -> substance denomination
    if gener_path is not None:
        for cis, toks in column_tokens(gener_path, 2, 1, keep).items():
            signals[cis] |= toks  # generic-group label -> its member CIS
    return signals


def percentile(values: list[float], pct: float) -> float:
    """Linear-interpolated percentile (numpy default method); pct in [0, 1]."""
    if not values:
        return 0.0
    ordered = sorted(values)
    k = (len(ordered) - 1) * pct
    lo = math.floor(k)
    hi = math.ceil(k)
    if lo == hi:
        return float(ordered[lo])
    return ordered[lo] * (hi - k) + ordered[hi] * (k - lo)


def load_frequency(
    path: Path,
) -> tuple[dict[str, float], list[tuple[frozenset[str], float]], float]:
    """Load a frequency list and return matchers plus the 25th-percentile score.

    The file is JSONL with at least ``term`` (a drug/substance name) and ``score``
    (higher = higher scrape priority), e.g.
    ``{"term": "DOLIPRANE", "type": "brand", "score": 10}``. Terms are normalised
    to tokens; single-word terms go into a fast ``{token: score}`` map and
    multi-word terms into a ``[(token_set, score)]`` list matched by subset (so
    "ACETYLSALICYLIQUE ACIDE" still matches "... ACIDE ACETYLSALICYLIQUE ..."). A
    term seen twice keeps its highest score.

    Returns ``(single, multi, p25)`` where ``p25`` is the 25th percentile of all
    scores, used as the fallback priority for drugs no term matches.
    """
    single: dict[str, float] = {}
    multi: list[tuple[frozenset[str], float]] = []
    scores: list[float] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        term, score = row.get("term"), row.get("score")
        if term is None or score is None:
            continue
        score = float(score)
        scores.append(score)
        toks = tokens(term)
        if len(toks) == 1:
            word = next(iter(toks))
            single[word] = max(single.get(word, score), score)
        elif toks:
            multi.append((frozenset(toks), score))
    return single, multi, percentile(scores, 0.25)


def score_catalog(
    signals: dict[str, set[str]],
    single: dict[str, float],
    multi: list[tuple[frozenset[str], float]],
    fallback: float,
) -> tuple[dict[str, float], set[str]]:
    """Score every CIS by the best frequency term matching its signal tokens.

    ``signals`` maps CIS -> token pool (name + substance + generic label, see
    ``substance_signals``). A CIS scores the max over: single-word terms present
    in its tokens, and multi-word terms whose whole token set is a subset of its
    tokens. A CIS no term matches gets ``fallback`` (the p25 priority), so it is
    still scraped at a middling rank rather than starved to the queue end.

    Returns ``(scores, matched)`` where ``matched`` is the set of CIS that hit a
    real term. This is tracked explicitly instead of inferred as ``score !=
    fallback``: many terms carry a score equal to the p25 fallback, so a genuine
    match at that score is indistinguishable from the fallback by value alone.
    """
    scores: dict[str, float] = {}
    matched: set[str] = set()
    for cis, toks in signals.items():
        best = max((single[t] for t in toks if t in single), default=None)
        for term_toks, score in multi:
            if (best is None or score > best) and term_toks <= toks:
                best = score
        if best is None:
            scores[cis] = fallback
        else:
            scores[cis] = best
            matched.add(cis)
    return scores, matched


def order_by_score(
    catalog: list[tuple[str, str]], scores: dict[str, float]
) -> list[str]:
    """Order CIS by descending score, breaking ties by original file order."""
    order = {cis: i for i, (cis, _) in enumerate(catalog)}
    return sorted((cis for cis, _ in catalog), key=lambda c: (-scores[c], order[c]))
