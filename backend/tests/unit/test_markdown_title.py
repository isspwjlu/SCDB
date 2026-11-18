"""Finding the paper title in a docling markdown document.

Every row of the ``papers`` table was written with the placeholder title
``(title unavailable) <doi>`` because the ingest path never passed one:
``upsert_paper`` was called with the DOI and the PDF path only. The title
was in the parsed markdown all along -- docling writes it as the
document's first heading -- so this module is what recovers it.
"""

from __future__ import annotations

import pytest

from superconductor_db.ingest.markdown_title import title_from_markdown


@pytest.mark.unit
def test_the_first_heading_is_the_title() -> None:
    md = (
        "## Superconductivity and electronic structures of nickelate films\n"
        "\n"
        "J. H. Ngai, R. Beck and G. Deutscher\n"
        "\n"
        "## Introduction\n"
        "text\n"
    )
    assert title_from_markdown(md) == (
        "Superconductivity and electronic structures of nickelate films"
    )


@pytest.mark.unit
def test_a_journal_banner_before_the_title_is_skipped() -> None:
    """docling emits the banner as a heading of its own."""
    md = (
        "## ORIGINAL ARTICLE\n"
        "\n"
        "## Optimization of penetration depth in submerged arc welding\n"
        "\n"
        "Ali Shafipour and Farhad Rahmati\n"
    )
    assert title_from_markdown(md) == (
        "Optimization of penetration depth in submerged arc welding"
    )


@pytest.mark.unit
def test_a_section_heading_is_not_mistaken_for_the_title() -> None:
    md = "## Abstract\n\nWe measured the transition temperature.\n"
    assert title_from_markdown(md) is None


@pytest.mark.unit
def test_a_numbered_section_heading_is_not_the_title() -> None:
    md = "## 1. Introduction\n\ntext\n"
    assert title_from_markdown(md) is None


@pytest.mark.unit
def test_the_title_survives_a_leading_image_comment() -> None:
    md = (
        "<!-- image -->\n"
        "\n"
        "## Towards Sustainable Concrete Rehabilitation\n"
        "\n"
        "Hanne Bielen and Bart Craeye\n"
    )
    assert title_from_markdown(md) == "Towards Sustainable Concrete Rehabilitation"


@pytest.mark.unit
def test_a_document_without_a_heading_falls_back_to_its_first_line() -> None:
    md = (
        "Superconductivity and charge-density-wave in the Holstein model\n"
        "\n"
        "Introduction- The discovery of quasicrystal has spurred\n"
    )
    assert title_from_markdown(md) == (
        "Superconductivity and charge-density-wave in the Holstein model"
    )


@pytest.mark.unit
def test_the_fallback_does_not_pick_an_author_line() -> None:
    md = (
        "Huigang Zhang, Tan Shi, Paul V Braun, and Zhang, and Dong, and "
        "Wei, and Kang, and others, and more, and yet more, and still more\n"
        "\n"
        "The first paragraph of the paper.\n"
    )
    assert title_from_markdown(md) is None


@pytest.mark.unit
def test_a_document_without_a_title_reports_none() -> None:
    assert title_from_markdown("1\n\n2\n\n3\n") is None
    assert title_from_markdown("") is None


@pytest.mark.unit
def test_the_numbering_and_the_emphasis_are_stripped() -> None:
    md = "## **Superconductivity in La3Ni2O7**\n"
    assert title_from_markdown(md) == "Superconductivity in La3Ni2O7"


@pytest.mark.unit
def test_a_date_line_is_not_a_title() -> None:
    md = "Received: 13 January 2024 / Accepted: 31 May 2024 / Published: 8 June\n"
    assert title_from_markdown(md) is None
