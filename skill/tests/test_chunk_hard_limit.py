from chunker import _chunk_document, chunk_document


def test_protected_blocks_are_bounded_without_text_loss():
    for kind in ("text", "xlsx", "image"):
        text = "```\n" + "中文😀abc" * 4000 + "\n```"
        before = _chunk_document(text, doc_type=kind)
        after = chunk_document(text, doc_type=kind)
        assert "".join(c.text for c in before) == "".join(c.text for c in after)
        assert all(len(c.text) <= 1200 and len(c.text.encode("utf-8")) <= 1800 for c in after)
        assert len({c.chunk_id for c in after}) == len(after)
