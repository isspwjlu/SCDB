"""Unit tests for the prompt builders and the paper condenser."""

from __future__ import annotations

import pytest

from superconductor_db.ingest.prompts import (
    build_base_sc_props_prompt,
    build_composition_prompt,
    condense_for_prompt,
)


@pytest.mark.unit
def test_condense_keeps_short_papers_untouched() -> None:
    text = "## Abstract\n\nMgB2 superconducts at 39 K.\n"
    assert condense_for_prompt(text) == text


@pytest.mark.unit
def test_condense_keeps_tables_headings_and_measurements() -> None:
    filler = "The sample was prepared by arc melting. " * 400
    text = (
        "# Title\n\nAuthors\n\n"
        + filler
        + "\n## Results\n\n"
        + "| Material | Tc |\n|---|---|\n| MgB2 | 39 K |\n"
        + "\n"
        + filler
        + "\nThe transition was observed at 39 K under zero field.\n"
    )
    out = condense_for_prompt(text)
    assert "| Material | Tc |" in out
    assert "| MgB2 | 39 K |" in out
    assert "## Results" in out
    assert "observed at 39 K" in out
    # The bulk of the filler prose is gone.
    assert len(out) < len(text) / 2


@pytest.mark.unit
def test_condense_output_is_a_subset_of_the_input() -> None:
    """The evidence gate searches the full text, so the condensed version
    must not introduce lines the paper does not have -- apart from the
    table marker."""
    filler = "Nothing to see here. " * 500
    text = f"# T\n\n{filler}\n\n| a | b |\n|---|---|\n| 1 | 2 |\n"
    out = condense_for_prompt(text)
    for line in out.splitlines():
        stripped = line.strip()
        if not stripped or stripped == "[TABLE]":
            continue
        assert stripped in text


@pytest.mark.unit
def test_condense_respects_the_character_budget() -> None:
    text = "## S\n\n" + ("value 39 K here. " * 5000)
    out = condense_for_prompt(text, max_chars=5000)
    assert len(out) <= 5000


@pytest.mark.unit
def test_prompt_builders_embed_the_paper() -> None:
    text = "## Results\n\nMgB2 superconducts at 39 K.\n"
    for messages in (
        build_composition_prompt(text, doi="10.1000/x"),
        build_base_sc_props_prompt(text, materials=["MgB2"], doi="10.1000/x"),
    ):
        assert messages[0]["role"] == "system"
        assert "MgB2 superconducts at 39 K." in messages[1]["content"]
        assert "10.1000/x" in messages[1]["content"]
