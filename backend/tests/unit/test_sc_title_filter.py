"""The superconductor title gate.

The gate decides which discovered papers are worth a download slot and a
run of LLM calls. It has to keep the measurement papers and reject the
material-science papers that keyword searches keep surfacing.
"""

from __future__ import annotations

import pytest

from superconductor_db.ingest.paper_search_loop import (
    _looks_like_sc_measurement_title,
    _looks_like_sc_title,
)


@pytest.mark.unit
def test_a_bare_bi_inside_a_word_is_not_a_bismuth_compound() -> None:
    """``Bi2?2?1?2?`` had every digit optional, so it matched the "bi" of
    "Orbital" -- and a spintronics paper with "magnetization" in its title
    then passed the superconductor gate on that word alone."""
    title = (
        "Efficient Magnetization Switching Driven by Orbital Torque in "
        "Light 3d Transition-Metal Nitrides"
    )
    assert _looks_like_sc_title(title) is False
    assert _looks_like_sc_measurement_title(title) is False


@pytest.mark.unit
def test_the_bismuth_compound_still_passes() -> None:
    assert _looks_like_sc_title(
        "Superconductivity in Bi-2212 thin films"
    ) is True
    assert _looks_like_sc_title(
        "Enhanced critical temperature of Bi2212 ceramics"
    ) is True


@pytest.mark.unit
def test_anisotropic_is_not_isotropic() -> None:
    """``isotropic`` sat inside "anisotropic", so the theory filter threw
    away the anisotropic upper-critical-field papers -- 213 of the 233
    corpus titles that carry the word were the anisotropic ones."""
    assert _looks_like_sc_measurement_title(
        "Anisotropic upper critical field in the van der Waals "
        "superconducting quaternary hydride"
    ) is True
    assert _looks_like_sc_measurement_title(
        "Single crystal MgB2 with anisotropic superconducting properties"
    ) is True


@pytest.mark.unit
def test_the_isotropic_case_is_still_rejected() -> None:
    assert _looks_like_sc_measurement_title(
        "Isotropic critical temperature of the superconducting state"
    ) is False


@pytest.mark.unit
def test_a_vortex_measurement_paper_passes() -> None:
    """Vortex dynamics and flux flow are measurement topics, not theory.

    They were in the veto list, and the papers they rejected produced
    records more often (35% and 40%) than the papers that survived it
    (23%) -- so the veto was costing more than it saved.
    """
    assert _looks_like_sc_measurement_title(
        "Vortex lattice and the critical temperature in superconducting MgB2"
    ) is True
    assert _looks_like_sc_measurement_title(
        "Flux flow resistivity and the critical temperature of a "
        "type-II superconductor"
    ) is True


@pytest.mark.unit
def test_emergent_superconductivity_passes() -> None:
    """``emergent`` vetoed every title that used the word, including the
    ones that report a transition temperature."""
    assert _looks_like_sc_measurement_title(
        "Emergent superconductivity in La3Ni2O7 at ambient pressure"
    ) is True


@pytest.mark.unit
def test_a_measurement_paper_passes() -> None:
    assert _looks_like_sc_measurement_title(
        "Zero resistance and the critical temperature of La3Ni2O7"
    ) is True


@pytest.mark.unit
def test_a_review_is_rejected() -> None:
    assert _looks_like_sc_measurement_title(
        "A review of iron-based superconductors"
    ) is False


@pytest.mark.unit
def test_a_theory_paper_is_rejected() -> None:
    assert _looks_like_sc_measurement_title(
        "First-principles study of the superconducting state in hydrides"
    ) is False
