"""Bounded excerpt selection within retrieved semantic passages.

The returned offset addresses the original string, not the decorated excerpt.
This module does not change retrieval ranking or any stored evidence.
"""

import re


def select_snippet(text: str, query: str, limit: int = 800) -> tuple[int, str]:
    """Choose a window covering distinct query terms, favoring explicit phrases.

    Work is bounded to 32 distinct query units, 32 occurrences per unit, and
    the first 128,000 text characters. Callers can pass a retrieved semantic
    chunk and add its start to the returned offset. With no overlap, return
    the beginning unchanged. Short terms have less weight without a language-
    specific stopword list; repeated occurrences do not add relevance.
    """
    if limit < 1:
        raise ValueError("snippet limit must be positive")
    units = {}
    for phrase, word in re.findall(r'"([^\"]+)"|(\S+)', query[:4096]):
        value = phrase or word.rstrip("?!,;:")
        if not value or value.casefold() in units:
            continue
        units[value.casefold()] = (value, bool(phrase))
        if len(units) == 32:
            break
    matches = []
    for unit, (value, phrase) in enumerate(units.values()):
        pattern = ((r"(?<!\w)" if value[0].isalnum() or value[0] == "_" else "")
                   + re.escape(value)
                   + (r"(?!\w)" if value[-1].isalnum() or value[-1] == "_" else ""))
        weight = min(len(value), 12) * (3 if phrase else 1)
        for count, match in enumerate(re.finditer(pattern, text[:128000], re.IGNORECASE)):
            if count == 32:
                break
            matches.append((match.start(), match.end(), unit, weight, phrase))
    if not matches:
        return 0, text[:limit] + ("…" if len(text) > limit else "")

    # Include both context-centered and match-aligned windows: the latter can
    # retain a cluster that would be split by the usual preceding context.
    starts = {max(0, start - limit // 4) for start, *_ in matches}
    starts.update(start for start, *_ in matches)
    best = None
    for start in sorted(starts):
        covered = [m for m in matches if start <= m[0] and m[1] <= start + limit]
        if not covered:
            continue
        weights = {m[2]: m[3] for m in covered}
        phrases = {m[2] for m in covered if m[4]}
        anchor = min(m[0] for m in covered)
        score = (len(phrases), sum(weights.values()), len(weights), -start)
        if best is None or score > best[0]:
            best = score, start, anchor
    if best is None:  # A query unit can be longer than the entire excerpt.
        anchor = min(m[0] for m in matches)
        start = max(0, anchor - limit // 4)
    else:
        _, start, anchor = best
    snippet = (("…" if start else "") + text[start:start + limit]
               + ("…" if start + limit < len(text) else ""))
    return anchor, snippet
