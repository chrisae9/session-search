"""Rank bounded evidence pools without changing their filters or citations."""

import re


def evidence_weight(text, role, query):
    """Prefer exchanges over execution scaffolding for ordinary recollection.

    These are ranking hints, never exclusions. Literal retrieval bypasses them;
    explicit role and planning queries retain their requested type of evidence.
    """
    weight = 1.0 if query.role else {"tool": .2, "context": .2, "user": .75}.get(role, 1.0)
    if re.match(r"\s*<(?:subagent_notification|system|environment_context|turn_aborted)\b", text):
        weight *= .1
    # An old invocation of this very question is not its answer. Keep exact
    # command/identifier queries available through the literal path.
    if role == "tool" and len(query.text.split()) >= 5 and query.text.casefold() in text.casefold():
        weight *= .1
    if (role == "assistant"
            and not re.search(r"\b(plan|planning|next|intend|proposal|propose)\b", query.text, re.I)
            and re.match(r"(?:I(?:'m|’m| am| will|'ll|’ll)|Let me|Yes[—, ]+I(?:'ll|’ll)|"
                         r"The next (?:step|layer))\b", text)):
        weight *= .4
    if (re.search(r"gold set|benchmark|recall@|\bMRR\b", text, re.I)
            and not re.search(r"benchmark|evaluat|ranking|recall|\bMRR\b", query.text, re.I)):
        weight *= .2
    return weight


def rank_results(query, lexical, semantic=()):
    """Fuse ranks, then expose distinct conversations before repeated hits.

    A small reciprocal-rank constant retains the distinction between the head
    and tail of each candidate list. Evidence weights do not alter membership.
    Scoped searches keep every matching passage in ordinary score order.
    """
    merged, scores = {}, {}
    for candidates in (lexical, semantic):
        for rank, result in enumerate(candidates, 1):
            cite = result["citation"]
            key = (cite["session_id"], cite["revision"], cite["event_id"])
            merged[key] = result
            scores[key] = scores.get(key, 0) + 1 / (1 + rank)
    for key, result in merged.items():
        scores[key] *= result.get("_evidence_weight", 1.0)
    ordered = sorted(scores, key=lambda key: (
        key[0] != query.text.strip(), -scores[key], key,
    ))
    if not query.session_id:
        seen, distinct, repeated = set(), [], []
        for key in ordered:
            if key[0] in seen:
                repeated.append(key)
            else:
                seen.add(key[0])
                distinct.append(key)
        ordered = distinct + repeated
    return [{**{k: v for k, v in merged[key].items() if k != "_evidence_weight"},
             "score": scores[key]} for key in ordered]
