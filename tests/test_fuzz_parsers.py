"""Fuzz / property tests for the document parsers and chunker.

These assert *robustness*, not correctness of extracted text: the dependency-free
ingestion paths (plain text, CSV/TSV, JSON, HTML, ``.eml``) and the RAG chunker
must never crash on malformed, adversarial, or random input — they should always
return a well-typed result so a single poisoned document can never take down an
ingestion run. Hypothesis explores the input space; a deterministic stdlib fuzz
loop runs as a belt-and-suspenders smoke test even if Hypothesis shrinks oddly.
"""
from __future__ import annotations

import random

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from hybridagent import ingest
from hybridagent.rag import chunk_text

# Suffixes whose parsers must run with the standard library alone.
TEXT_SUFFIXES = sorted(ingest.TEXT_SUFFIXES)
DEPFREE_RICH = [".eml", ".html", ".htm"]
DEPFREE_ALL = TEXT_SUFFIXES + DEPFREE_RICH

_FUZZ = settings(
    deadline=None,
    max_examples=40,
    derandomize=True,  # deterministic example selection -> reproducible CI runs
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)


# --------------------------------------------------------------- chunk_text
@settings(deadline=None, max_examples=200, derandomize=True)
@given(
    text=st.text(),
    chunk_size=st.integers(min_value=1, max_value=2000),
    overlap=st.integers(min_value=0, max_value=2000),
)
def test_chunk_text_never_crashes(text, chunk_size, overlap):
    chunks = chunk_text(text, chunk_size=chunk_size, overlap=overlap)
    assert isinstance(chunks, list)
    assert all(isinstance(c, str) for c in chunks)
    # Empty / whitespace-only input yields no chunks; real content yields some.
    if text.strip():
        assert chunks
    else:
        assert chunks == []


@settings(deadline=None, max_examples=200, derandomize=True)
@given(
    text=st.text(min_size=1),
    chunk_size=st.integers(min_value=1, max_value=1000),
    overlap=st.integers(min_value=0, max_value=1000),
)
def test_chunk_text_size_is_bounded(text, chunk_size, overlap):
    """No chunk may explode past chunk_size + the (clamped) overlap tail."""
    chunks = chunk_text(text, chunk_size=chunk_size, overlap=overlap)
    eff_overlap = max(0, min(overlap, chunk_size // 2))
    cap = chunk_size + eff_overlap + 2  # +1 join space, +1 slack
    assert all(len(c) <= cap for c in chunks)


def test_chunk_text_pathological_overlap_terminates():
    # overlap >= chunk_size must not loop forever or explode to one-char chunks.
    chunks = chunk_text("x" * 5000, chunk_size=100, overlap=10_000)
    assert chunks
    assert all(len(c) <= 152 for c in chunks)  # 100 + clamped overlap(50) + 2


# --------------------------------------------------- text-suffix robustness
@_FUZZ
@given(data=st.binary(max_size=4096), suffix=st.sampled_from(TEXT_SUFFIXES))
def test_text_parsers_never_crash_on_random_bytes(tmp_path, data, suffix):
    p = tmp_path / f"fuzz{suffix}"
    p.write_bytes(data)
    doc = ingest.extract_text(p)
    assert isinstance(doc.text, str)
    assert doc.source == p.name


@_FUZZ
@given(data=st.binary(max_size=4096), suffix=st.sampled_from(DEPFREE_RICH))
def test_depfree_rich_parsers_never_crash_on_random_bytes(tmp_path, data, suffix):
    p = tmp_path / f"fuzz{suffix}"
    p.write_bytes(data)
    doc = ingest.extract_text(p)
    assert isinstance(doc.text, str)


@_FUZZ
@given(text=st.text(max_size=4096), suffix=st.sampled_from(DEPFREE_ALL))
def test_parsers_never_crash_on_random_unicode(tmp_path, text, suffix):
    p = tmp_path / f"uni{suffix}"
    p.write_text(text, encoding="utf-8")
    doc = ingest.extract_text(p)
    assert isinstance(doc.text, str)


# ----------------------------------------- structured-but-hostile HTML / CSV
@_FUZZ
@given(
    tags=st.lists(
        st.sampled_from(
            ["<script>", "</script>", "<style>", "</style>", "<p>", "</p>",
             "<b>", "<!--", "-->", "<", ">", "</", "/>", "<svg>", "&amp;",
             "<img src=x onerror=1>", "\x00", "<a href='javascript:'>"]
        ),
        max_size=60,
    )
)
def test_html_parser_survives_hostile_markup(tmp_path, tags):
    p = tmp_path / "hostile.html"
    p.write_text("".join(tags), encoding="utf-8")
    out = ingest.extract_text(p).text
    # Robustness only: extraction must not crash and must return text. Whether a
    # raw "<script>" fragment survives depends on the active backend (stdlib
    # HTMLParser vs. an optional converter like markitdown), so script-stripping
    # correctness is asserted on well-formed input in test_ingest.py instead.
    assert isinstance(out, str)


@_FUZZ
@given(
    rows=st.lists(
        st.lists(st.text(max_size=20), max_size=8), max_size=40
    )
)
def test_csv_parser_survives_arbitrary_rows(tmp_path, rows):
    body = "\n".join(",".join(c.replace("\n", " ") for c in r) for r in rows)
    p = tmp_path / "fuzz.csv"
    p.write_text(body, encoding="utf-8")
    assert isinstance(ingest.extract_text(p).text, str)


# ------------------------------------------------ deterministic stdlib fuzz
def test_deterministic_random_fuzz(tmp_path):
    """Belt-and-suspenders: 300 random blobs across every dep-free suffix."""
    rnd = random.Random(0xC0FFEE)
    for i in range(300):
        suffix = rnd.choice(DEPFREE_ALL)
        n = rnd.randint(0, 3000)
        blob = bytes(rnd.randint(0, 255) for _ in range(n))
        p = tmp_path / f"r{i}{suffix}"
        p.write_bytes(blob)
        doc = ingest.extract_text(p)
        assert isinstance(doc.text, str), f"non-str text for {suffix}"


def test_null_bytes_in_every_text_format(tmp_path):
    """NUL bytes are a classic csv.reader landmine; all formats must absorb them."""
    for suffix in DEPFREE_ALL:
        p = tmp_path / f"nul{suffix}"
        p.write_bytes(b"col1,col2\nval\x00ue,a\x00b\n{\x00}")
        doc = ingest.extract_text(p)
        assert isinstance(doc.text, str)


def test_unsupported_suffix_still_raises(tmp_path):
    p = tmp_path / "x.bin"
    p.write_bytes(b"\x00\x01\x02")
    with pytest.raises(ValueError):
        ingest.extract_text(p)
