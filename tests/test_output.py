"""Response budgets terminate while preserving immutable evidence addresses."""

from copy import deepcopy

import pytest

from session_search.core import output
from session_search.core.records import canonical_json


@pytest.mark.parametrize("length", [128, 129, 130, 259, 4096])
@pytest.mark.parametrize("field", ["excerpt", "text"])
def test_tight_budget_makes_progress_at_minimum_text_length(monkeypatch, length, field):
    citation = {"session_id": "s", "revision": "r", "event_id": "e", "offset": 0}
    item = {"citation": citation}
    if field == "excerpt":
        item[field] = "x" * length
    else:
        item["events"] = [{"event_id": "e", "text": "x" * length}]
    response = {"results": [item], "metadata": "m" * 850}
    original = deepcopy(response)
    calls = 0

    def bounded_serialization(value):
        nonlocal calls
        calls += 1
        # Make the pre-fix liveness regression fail promptly, not hang pytest.
        assert calls < 50, "budget loop stopped making progress"
        return canonical_json(value)

    monkeypatch.setattr(output, "canonical_json", bounded_serialization)
    delivered = output.bounded_response(response, 1024)
    assert len(canonical_json(delivered).encode()) <= 1024
    assert delivered["results"] == []
    assert delivered["omitted_results"] == 1
    assert delivered["truncated"] is True
    assert response == original


@pytest.mark.parametrize("field", ["excerpt", "text"])
def test_shortening_preserves_source_prefix_and_citation(field):
    citation = {"session_id": "s", "revision": "immutable-r", "event_id": "e", "offset": 37}
    source = "Ωé mixed content " * 400
    item = {"citation": citation}
    if field == "excerpt":
        item[field] = source
    else:
        item["events"] = [{"event_id": "e", "text": source, "text_offset": 37}]
    response = {"results": [item]}
    original = deepcopy(response)
    delivered = output.bounded_response(response, 1024)
    assert len(canonical_json(delivered).encode()) <= 1024
    hit = delivered["results"][0]
    assert hit["citation"] == citation
    evidence = hit if field == "excerpt" else hit["events"][0]
    assert evidence["text_truncated"] is True
    assert evidence[field].endswith("…")
    assert source.startswith(evidence[field][:-1])
    if field == "text":
        assert evidence["text_offset"] == 37
    assert response == original


def test_unshrinkable_metadata_raises_without_mutating_input():
    response = {"metadata": "x" * 2000, "results": []}
    original = deepcopy(response)
    with pytest.raises(ValueError, match="metadata exceeds"):
        output.bounded_response(response, 1024)
    assert response == original
