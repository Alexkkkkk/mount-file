# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""Focused tests for the optional documentation retriever."""

from mountfile.ai.rag import NO_EVIDENCE_MESSAGE, DocumentationRetriever


def test_indexes_markdown_and_docstrings_with_citations_but_not_images(tmp_path):
    (tmp_path / "README.md").write_text(
        "# Mount File\n\nGeneral project text.\n\n"
        "## Configuration\n\nSet the AI backend in Settings.\n",
        encoding="utf-8",
    )
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n\n## 1.0\n\nAdded documentation search.\n",
        encoding="utf-8",
    )
    package = tmp_path / "mountfile"
    package.mkdir()
    (package / "feature.py").write_text(
        '"""Feature module for safe documentation retrieval."""\n\n'
        "def configure():\n"
        '    """Configure the local AI backend without reading image data."""\n'
        "    return None\n",
        encoding="utf-8",
    )
    # An image may contain words that look relevant, but it is not an allowed
    # documentation source and must never be opened by the indexer.
    (tmp_path / "secret.img").write_bytes(b"Settings AI backend secret image evidence")

    retriever = DocumentationRetriever(tmp_path)
    results = retriever.retrieve("AI backend configuration", top_k=10)

    assert results
    assert any(item.source == "README.md" for item in results)
    readme = next(item for item in results if item.source == "README.md")
    assert readme.section == "Configuration"
    assert readme.start_line == 5
    assert readme.end_line == 7
    assert "README.md" in readme.citation
    assert "Configuration" in readme.citation
    assert "lines 5-7" in readme.citation
    assert all(item.source != "secret.img" for item in results)
    assert any(item.source == "mountfile/feature.py" for item in retriever.chunks)


def test_answer_cites_snippets_and_is_explicit_when_no_evidence(tmp_path):
    (tmp_path / "README.md").write_text(
        "# Usage\n\nRun `mountfile image.img` to inspect an image.\n",
        encoding="utf-8",
    )
    retriever = DocumentationRetriever(tmp_path)

    answer = retriever.answer("how do I inspect an image?")
    assert "Evidence from indexed documentation:" in answer
    assert "mountfile image.img" in answer
    assert "[README.md" in answer
    assert "Usage" in answer
    assert "lines 1-3" in answer

    assert retriever.retrieve("unicorns and completely unrelated text") == []
    assert retriever.answer("unicorns and completely unrelated text") == NO_EVIDENCE_MESSAGE


def test_indexes_repository_docs_markdown(tmp_path):
    (tmp_path / "README.md").write_text("# Project\n\nOverview.\n", encoding="utf-8")
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "api.md").write_text(
        "# API\n\nUse the authenticated filesystem endpoint.\n", encoding="utf-8"
    )
    retriever = DocumentationRetriever(tmp_path)

    results = retriever.retrieve("authenticated filesystem endpoint", top_k=5)

    assert any(item.source == "docs/api.md" for item in results)


def test_injectable_embedding_backend_can_retrieve_without_token_overlap(tmp_path):
    (tmp_path / "README.md").write_text(
        "# Setup\n\nConfigure the network adapter here.\n\n"
        "# Other\n\nThis section discusses unrelated storage.\n",
        encoding="utf-8",
    )

    class TinyBackend:
        def embed(self, text):
            lowered = text.casefold()
            if "network adapter" in lowered or "wireless" in lowered:
                return (1.0, 0.0)
            return (0.0, 1.0)

    retriever = DocumentationRetriever(tmp_path, embedding_backend=TinyBackend())
    results = retriever.retrieve("wireless setup")

    assert len(results) == 1
    assert results[0].section == "Setup"
    assert results[0].text == "# Setup\n\nConfigure the network adapter here."
