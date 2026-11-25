from datetime import datetime, timezone

from redmine_embedding.batch.chunking import chunk_document, split_text
from redmine_embedding.batch.sources.base import Section, SourceDocument

TRACE = "java.lang.IllegalStateException: x\n\tat a.B.c(B.java:1)\n\tat a.B.d(B.java:2)"


def doc(*sections, title="件名"):
    return SourceDocument(
        source_type="issue", source_id="1", project_id=1, title=title, url_path="/issues/1",
        updated_on=datetime(2026, 1, 1, tzinfo=timezone.utc), sections=list(sections),
    )


def test_split_text_packs_paragraphs_and_respects_limit():
    text = "\n\n".join(["あ" * 400] * 3)
    parts = split_text(text, 1000)
    assert len(parts) == 2 and all(len(p) <= 1000 for p in parts)


def test_split_text_breaks_long_paragraph_at_sentences_then_hard():
    sentence_text = "これは文です。" * 300  # one paragraph, no blank lines
    assert all(len(p) <= 100 for p in split_text(sentence_text, 100))
    assert all(len(p) <= 50 for p in split_text("x" * 500, 50))  # no break points at all


def test_chunk_numbers_are_sequential_and_titled():
    drafts = chunk_document(doc(Section("説明です"), Section("コメント", "#note-2")))
    assert [d.chunk_no for d in drafts] == [0, 1]
    assert drafts[0].embed_text == "件名\n説明です"
    assert drafts[1].url_fragment == "#note-2"


def test_title_not_duplicated_when_text_already_starts_with_it():
    drafts = chunk_document(doc(Section("件名\n\n本文")))
    assert drafts[0].embed_text.count("件名") == 1


def test_trace_becomes_its_own_chunk_with_normalised_embedding():
    drafts = chunk_document(doc(Section("落ちました\n\n" + TRACE + "\n\nお願いします")))
    assert len(drafts) == 3
    trace = drafts[1]
    assert trace.content == TRACE  # original kept for display
    assert "IllegalStateException" in trace.embed_text and ":1" not in trace.embed_text
    assert "件名" not in trace.embed_text  # traces are embedded on their own


def test_empty_document_yields_no_chunks():
    assert chunk_document(doc(Section("   "), title="")) == []
