from ctxgraph.config import ChunkingConfig
from ctxgraph.ingest.chunk import chunk_document

SMALL = ChunkingConfig(target_tokens=60, max_tokens=80, min_tokens=5, overlap_ratio=0.2)


def test_splits_by_heading_with_breadcrumbs():
    doc = (
        "# Title\n\nIntro paragraph that is long enough to stand on its own as a section body.\n\n"
        "## Alpha\n\nAlpha body text that is also long enough to not be merged away by min tokens.\n\n"
        "### Alpha child\n\nChild body text with enough words to count as a real section here.\n\n"
        "## Beta\n\nBeta body text long enough to stay separate from everything else in the doc.\n"
    )
    chunks = chunk_document("d.md", doc, SMALL)
    crumbs = [c.heading for c in chunks]
    assert crumbs == ["Title", "Title > Alpha", "Title > Alpha > Alpha child", "Title > Beta"]
    assert chunks[0].text.startswith("[d.md > Title]\n# Title")
    assert chunks[1].start_line == 5 and chunks[1].end_line == 7
    assert chunks[3].text.endswith("in the doc.")


def test_heading_inside_fence_does_not_split():
    doc = "# Doc\n\nText before fence.\n\n```python\n# not a heading\nx = 1\n```\n\nAfter.\n"
    chunks = chunk_document("d.md", doc, SMALL)
    assert len(chunks) == 1
    assert "# not a heading" in chunks[0].text


def test_tiny_sections_merge_into_neighbour():
    doc = "# A\n\n## B\n\nshort\n\n## C\n\nalso short\n"
    chunks = chunk_document("d.md", doc)
    assert len(chunks) == 1
    assert chunks[0].start_line == 1
    assert chunks[0].end_line == 9


def test_large_section_is_windowed_with_overlap_and_line_ranges():
    cfg = ChunkingConfig(target_tokens=60, max_tokens=80, min_tokens=5, overlap_ratio=0.3)
    paras = [f"Paragraph number {i} " + ("word " * 8) for i in range(12)]
    doc = "# Big\n\n" + "\n\n".join(paras) + "\n"
    chunks = chunk_document("d.md", doc, cfg)
    assert len(chunks) > 1
    for c in chunks:
        assert c.heading == "Big"
        body = c.text.split("\n", 1)[1]
        # every chunk stays within max_tokens of body
        assert (len(body) + 3) // 4 <= cfg.max_tokens + 5
    # windows are ordered, overlapping, and cover the whole section
    assert chunks[0].start_line == 1
    assert chunks[-1].end_line == len(doc.splitlines())
    lines = doc.splitlines()
    for prev, nxt in zip(chunks, chunks[1:]):
        assert nxt.start_line > prev.start_line
        # any gap between windows is blank lines only
        assert all(not l.strip() for l in lines[prev.end_line : nxt.start_line - 1])
    # the overlap re-emits a paragraph, so at least one paragraph shows up twice
    counts = {i: sum(f"Paragraph number {i} " in c.text for c in chunks) for i in range(12)}
    assert any(n == 2 for n in counts.values())


def test_oversized_single_block_is_hard_split():
    code = "```\n" + "\n".join(f"line_{i} = {i} " + "x" * 40 for i in range(60)) + "\n```\n"
    chunks = chunk_document("d.md", "# Code\n\n" + code, SMALL)
    assert len(chunks) > 1
    assert "line_0 " in chunks[0].text
    assert "line_59 " in chunks[-1].text


def test_plain_text_mode_ignores_headings():
    doc = "# looks like heading\n\nbut this is plain text\n"
    chunks = chunk_document("notes.txt", doc, headings=False)
    assert len(chunks) == 1
    assert chunks[0].heading == ""
    assert chunks[0].text.startswith("[notes.txt]\n")


def test_empty_document_yields_nothing():
    assert chunk_document("e.md", "") == []
    assert chunk_document("e.md", "\n\n   \n") == []
