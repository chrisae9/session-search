import pytest

from session_search.storage.snippets import select_snippet


def test_answer_cluster_beats_early_isolated_longest_term():
    text = ("Dependencies were considered. " + "Background discussion. " * 70
            + "The service lost disk mounts at boot; mount ordering fixed it.")
    offset, snippet = select_snippet(text, "dependencies service disk mounts boot", limit=160)
    assert "mount ordering fixed it" in snippet
    assert offset > text.index("Background")
    assert text[offset:].startswith("service")


def test_repeating_one_term_does_not_outvote_distinct_evidence():
    text = ("archive " * 100 + "Unrelated discussion. " * 30
            + "The archive recovery checksum verified every restored object.")
    _, snippet = select_snippet(text, "archive recovery checksum restored", limit=120)
    assert "verified every restored object" in snippet


def test_explicit_phrase_is_preserved_over_scattered_terms():
    text = ("restore archive checksum repaired " + "unrelated " * 60
            + "The exact error was out of memory during startup.")
    offset, snippet = select_snippet(text, 'restore archive checksum "out of memory"', limit=100)
    assert "out of memory" in snippet
    assert text[offset:].startswith("out of memory")


def test_unicode_offsets_address_original_text():
    text = "🧭 café İ ß. " * 40 + "Résumé recovery succeeded with verified checksums."
    offset, snippet = select_snippet(text, "résumé recovery checksums", limit=100)
    assert offset == text.index("Résumé")
    assert "Résumé recovery succeeded" in snippet


def test_no_overlap_short_text_and_invalid_limit():
    assert select_snippet("plain evidence", "elsewhere") == (0, "plain evidence")
    assert select_snippet("plain evidence", "elsewhere", 5) == (0, "plain…")
    assert select_snippet("", "query") == (0, "")
    with pytest.raises(ValueError, match="positive"):
        select_snippet("text", "query", 0)


def test_word_boundaries_do_not_match_inside_unrelated_words():
    text = "answer statement " * 30 + "We fixed the actual fault."
    offset, snippet = select_snippet(text, "we fault", limit=80)
    assert offset == text.index("We fixed")
    assert "actual fault" in snippet


def test_long_query_unit_keeps_valid_offset_and_bounded_excerpt():
    text = "prefix " + "x" * 100 + " suffix"
    offset, snippet = select_snippet(text, '"' + "x" * 100 + '"', limit=20)
    assert offset == 7
    assert len(snippet.strip("…")) == 20
