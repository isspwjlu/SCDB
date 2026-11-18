"""Unit tests for the SC extractor (Task 26).

We mock :class:`LLMClient` so the test runs offline and exercises only the
merge / sanity logic.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from superconductor_db.ingest.extractor import (
    SuperconductingExtractor,
    _is_valid_formula,
    _plausible_tc,
)
from superconductor_db.ingest.llm_client import LLMClient
from superconductor_db.schemas.material import SuperconductingRecord


def _client(responses: list[list[dict[str, Any]]]) -> LLMClient:
    """Build an LLMClient whose ``extract_json`` returns queued responses in order.

    The extractor issues the composition prompt first and then fans the seven
    dimension prompts out through ``asyncio.gather``, so the dimension calls
    arrive in a nondeterministic order. Queueing works for tests where the
    dimension payloads are interchangeable (all empty, or a single non-empty
    one identified by shape) — but a test that must attach a specific payload
    to a specific dimension should patch by prompt content instead.
    """
    c = LLMClient(settings=None) if False else object.__new__(LLMClient)
    c.settings = None  # not used by extract_json path
    queue = list(responses)

    async def fake_extract_json(messages):  # noqa: ANN001 - signature
        if not queue:
            raise AssertionError("LLM called more times than test provided")
        return queue.pop(0)

    c.extract_json = fake_extract_json  # type: ignore[method-assign]
    return c


def _client_by_prompt(responses: dict[str, list[dict[str, Any]]]) -> LLMClient:
    """Build a client that returns a payload keyed by a system-prompt phrase.

    Order-independent, so it is safe against the concurrent dimension fan-out.
    Keys are matched case-insensitively against the system message text.
    """
    c = LLMClient(settings=None) if False else object.__new__(LLMClient)
    c.settings = None
    lowered = {k.casefold(): v for k, v in responses.items()}

    async def fake_extract_json(messages):  # noqa: ANN001 - signature
        system = next(
            (m.get("content", "") for m in messages if m.get("role") == "system"), ""
        )
        for phrase, payload in lowered.items():
            if phrase in system.casefold():
                return payload
        return []

    c.extract_json = fake_extract_json  # type: ignore[method-assign]
    return c


@pytest.mark.unit
def test_plausible_tc_accepts_normal_values() -> None:
    assert _plausible_tc("YBa2Cu3O7", 92.0) is True
    assert _plausible_tc("MgB2", 39.0) is True


@pytest.mark.unit
def test_plausible_tc_rejects_implausible() -> None:
    assert _plausible_tc("Al", 100.0) is False
    assert _plausible_tc("Pb", 50.0) is False
    assert _plausible_tc("Anything", 500.0) is False


@pytest.mark.unit
def test_is_valid_formula_known_and_unknown() -> None:
    assert _is_valid_formula("YBa2Cu3O7") is True
    assert _is_valid_formula("MgB2") is True
    assert _is_valid_formula("FeSe0.5Te0.5") is True
    assert _is_valid_formula("Hg-1223") is True
    assert _is_valid_formula("XxYyZz") is False
    assert _is_valid_formula("") is False


@pytest.mark.unit
async def test_extract_all_merges_records() -> None:
    """End-to-end: given a fixed set of LLM payloads, returns one record.

    The paper text names all three specialised dimensions (isotope, Fermi,
    specific heat). Those prompts are only sent when the text shows a reason
    to, so without those cues the test would exercise four of seven builders
    and the isotope assertion below could not hold.
    """
    paper_text = (
        "YBa2Cu3O7 exhibits superconductivity at 92 K. We also report the "
        "isotope coefficient alpha, the Fermi temperature from de Haas-van "
        "Alphen oscillations, and the specific heat coefficient gamma."
    )

    responses: list[list[dict[str, Any]]] = [
        # 1. composition
        [
            {
                "Material_Chemical_Composition": "YBa2Cu3O7",
                "Experimental": True,
            }
        ],
        # 2. base_sc_props
        [
            {
                "YBa2Cu3O7": {
                    "Tc": {
                        "value": 92,
                        "unit": "K",
                        "evidence": "YBa2Cu3O7 exhibits superconductivity at 92 K",
                    },
                    "Superconductor_Family": "cuprate",
                }
            }
        ],
        # 3. pressure_tc
        [
            {
                "YBa2Cu3O7": {
                    "pressures": [0.0, 1.0, 2.0],
                    "tcs": [92, 94, 95],
                    "method": "resistivity",
                }
            }
        ],
        # 4. isotope_uemura
        [{"YBa2Cu3O7": {"isotope_exponent_alpha": 0.5, "uemura_class": "weak-coupling"}}],
        # 5. specific_heat
        [{"YBa2Cu3O7": {"gamma_n": {"value": 5.0, "unit": "mJ/mol.K^2"}}}],
        # 6. fermi_resistivity
        [{"YBa2Cu3O7": {"fermi_temperature": {"value": 250.0, "unit": "K"}}}],
        # 7. crystal_structure
        [{"YBa2Cu3O7": {"Crystal_Structure": "perovskite", "Space_Group": "Pmmm"}}],
        # 8. preparation
        [{"YBa2Cu3O7": {"Synthesis_Method": "solid-state reaction"}}],
    ]

    # The set of dimension prompts sent depends on what the paper text
    # mentions, so responses are matched by prompt content rather than by
    # call order.
    by_prompt = {
        "superconductor property measurements": responses[1],
        "pressure-tuned Tc": responses[2],
        "isotope-effect": responses[3],
        "specific-heat": responses[4],
        "Fermi-temperature": responses[5],
        "crystal-structure": responses[6],
        "synthesis/preparation": responses[7],
    }
    by_prompt[
        "extracting superconductor compositions"
    ] = responses[0]
    extractor = SuperconductingExtractor(_client_by_prompt(by_prompt))
    records = await extractor.extract_all(paper_text, doi="10.1234/test")

    assert len(records) == 1
    rec = records[0]
    assert rec.material_chemical_composition == "YBa2Cu3O7"
    assert rec.superconducting_property.Tc is not None
    assert rec.superconducting_property.Tc.value == 92
    assert rec.superconducting_property.Superconductor_Family == "cuprate"
    assert rec.tc_under_pressure is not None
    assert rec.tc_under_pressure.pressures == [0.0, 1.0, 2.0]
    assert rec.isotope_exponent_alpha == 0.5
    assert rec.uemura_class == "weak-coupling"
    assert rec.structure_information.crystal_structure == "perovskite"
    assert rec.preparation_method.Synthesis_Method == "solid-state reaction"


@pytest.mark.unit
async def test_extract_all_drops_empty_records() -> None:
    """If a record has no SC quantity it must be filtered out."""
    responses: list[list[dict[str, Any]]] = [
        [{"Material_Chemical_Composition": "YBa2Cu3O7", "Experimental": True}],
        [{"YBa2Cu3O7": {}}],  # base props - empty
        [], [], [], [], [],  # other prompts empty
    ]
    client = _client(responses)
    extractor = SuperconductingExtractor(client)
    records = await extractor.extract_all("dummy")
    assert records == []


@pytest.mark.unit
async def test_extract_all_drops_invalid_formula() -> None:
    responses: list[list[dict[str, Any]]] = [
        [{"Material_Chemical_Composition": "XxYyZz", "Experimental": True}],
        [], [], [], [], [], [],
    ]
    client = _client(responses)
    extractor = SuperconductingExtractor(client)
    records = await extractor.extract_all("dummy")
    assert records == []


@pytest.mark.unit
async def test_extract_all_continues_on_partial_failure() -> None:
    """If one of the dimension prompts raises, the others still flow through.

    base_sc_props is the prompt that seeds the inclusion gate (Tc, Hc1, Hc2...).
    If THAT one raises, no record survives — that's correct behaviour, not a
    bug. To exercise "partial failure → still get a record", we let
    base_sc_props succeed and let a secondary prompt (pressure_tc) raise.
    """
    paper_text = "MgB2 paper with a measured transition at 39 K."
    dimension_payloads = {
        "base_sc_props": [
            {
                "MgB2": {
                    "Tc": {
                        "value": 39,
                        "unit": "K",
                        "evidence": "a measured transition at 39 K",
                    }
                }
            }
        ],
        "pressure_tc": [{"MgB2": {"pressures": [0, 1], "tcs": [39, 42], "method": "resistivity"}}],
        "isotope_uemura": [{"MgB2": {"isotope_exponent_alpha": 0.32, "uemura_class": "weak-coupling"}}],
        "specific_heat": [{"MgB2": {"gamma_n": {"value": 11, "unit": "mJ/mol·K²"},
                                       "gamma_s": {"value": 14, "unit": "mJ/mol·K²"},
                                       "debye_temperature": {"value": 800, "unit": "K"}}}],
        "fermi_resistivity": [{"MgB2": {"fermi_temperature": {"value": 800, "unit": "K"},
                                          "resistivity_normal": {"value": 5, "unit": "μΩ·cm"}}}],
        "crystal_structure": [{"MgB2": {"Crystal_Structure": "hexagonal", "Space_Group": "P6/mmm"}}],
        "preparation": [{"MgB2": {"Synthesis_Method": "solid-state reaction"}}],
    }
    builders_in_order = list(dimension_payloads.values())
    # The dimension that will fail (idx 3 = specific_heat in the parallel batch).
    fail_index = 3

    async def selective(messages):  # noqa: ANN001
        if not hasattr(selective, "n"):
            selective.n = 0
        selective.n += 1
        if selective.n == 1:
            return [{"Material_Chemical_Composition": "MgB2", "Experimental": True}]
        idx = selective.n - 2  # calls 2..8 map to builders_in_order[0..6]
        if idx == fail_index:
            raise RuntimeError("simulated specific_heat failure")
        if 0 <= idx < len(builders_in_order):
            return builders_in_order[idx]
        return []

    client = object.__new__(LLMClient)
    client.extract_json = selective  # type: ignore[method-assign]
    extractor = SuperconductingExtractor(client)
    records = await extractor.extract_all(paper_text)
    assert len(records) == 1
    assert records[0].material_chemical_composition == "MgB2"
    # Tc survived (base_sc_props succeeded).
    assert records[0].superconducting_property.Tc.value == 39.0
    # Inclusion gate passed (Tc is real).
    assert records[0].has_meaningful_data() is True

@pytest.mark.unit
async def test_extract_all_drops_values_without_evidence() -> None:
    """Values the model cannot quote from the paper must be discarded.

    This is the guard against textbook recall: an LLM that "knows" Hc2 = 14 T
    for MgB2 would otherwise stamp that number onto every paper merely
    mentioning MgB2, which is what produced 30 identical MgB2 rows.
    """
    paper_text = (
        "We measured a superconducting transition in MgB2 thin films, where "
        "resistivity vanishes below Tc = 39 K. The upper critical field "
        "Hc2(0) reached 14 T at 0 K."
    )
    responses: list[list[dict[str, Any]]] = [
        [{"Material_Chemical_Composition": "MgB2", "Experimental": True}],
        [
            {
                "MgB2": {
                    # Supported: the quote occurs in the paper.
                    "Tc": {
                        "value": 39,
                        "unit": "K",
                        "evidence": "resistivity vanishes below Tc = 39 K",
                    },
                    # Recalled, not stated in the paper -> must be dropped.
                    "Hc2": {"value": 70, "unit": "T", "evidence": "NA"},
                    "Energy_Gap": {
                        "value": 7.2,
                        "unit": "meV",
                        "evidence": "a value we invented for the model",
                    },
                }
            }
        ],
        [], [], [], [], [],
    ]
    client = _client(responses)
    extractor = SuperconductingExtractor(client)
    records = await extractor.extract_all(paper_text)

    assert len(records) == 1
    sp = records[0].superconducting_property
    assert sp.Tc is not None and sp.Tc.value == 39
    assert sp.Hc2 is None
    assert sp.Energy_Gap is None


@pytest.mark.unit
async def test_extract_all_keeps_evidence_despite_punctuation_drift() -> None:
    """Quotes must survive PDF whitespace/hyphenation noise."""
    paper_text = (
        "Samples of YBa2Cu3O7-delta were grown by solid state reaction. "
        "The zero-resistance transition occurred at 92 K."
    )
    responses: list[list[dict[str, Any]]] = [
        [{"Material_Chemical_Composition": "YBa2Cu3O7", "Experimental": True}],
        [
            {
                "YBa2Cu3O7": {
                    "Tc": {
                        "value": 92,
                        "unit": "K",
                        "evidence": "the zero-resistance transition occurred at 92 K",
                    }
                }
            }
        ],
        [], [], [], [], [],
    ]
    client = _client(responses)
    extractor = SuperconductingExtractor(client)
    records = await extractor.extract_all(paper_text)
    assert len(records) == 1
    assert records[0].superconducting_property.Tc.value == 92


@pytest.mark.unit
async def test_extract_all_accepts_hyphenated_pdf_evidence() -> None:
    """Real evidence must survive PDF line-break hyphenation.

    Extracted PDF text hyphenates words at column breaks ("cova-lent",
    "Fi-nally"). Stripping punctuation without rejoining those first makes
    the paper text unmatchable against a correctly-spelled quote, so genuine
    measurements were being rejected by the evidence gate.
    """
    paper_text = (
        "The Fm-3m-LaH10 structure at 150 GPa, where we observe a "
        "resistive transition at 250 K, is the cova- lent Im-3m-H3S "
        "structure at 150 and 200 GPa."
    )
    client = _client_by_prompt(
        {
            "expert materials scientist extracting superconductor compositions": [
                {"Material_Chemical_Composition": "LaH10", "Experimental": True}
            ],
            "extract superconductor property measurements": [
                {
                    "LaH10": {
                        "Tc": {
                            "value": 250,
                            "unit": "K",
                            "evidence": "we observe a resistive transition at 250 K",
                        }
                    }
                }
            ],
        }
    )
    records = await SuperconductingExtractor(client).extract_all(paper_text)
    assert len(records) == 1
    assert records[0].superconducting_property.Tc.value == 250


@pytest.mark.unit
async def test_extract_all_rejects_short_symbol_only_evidence() -> None:
    """A quantity symbol plus a number is not evidence of anything.

    "Tc 200 K" would match any superconductivity paper that prints 200 K
    somewhere, which is the textbook-recall failure the gate exists to stop.
    """
    paper_text = (
        "Superconductivity was observed below 200 K in the doped sample and "
        "the transition was sharp."
    )
    client = _client_by_prompt(
        {
            "expert materials scientist extracting superconductor compositions": [
                {"Material_Chemical_Composition": "LaH10", "Experimental": True}
            ],
            "extract superconductor property measurements": [
                {
                    "LaH10": {
                        "Tc": {"value": 200, "unit": "K", "evidence": "Tc 200 K"}
                    }
                }
            ],
        }
    )
    records = await SuperconductingExtractor(client).extract_all(paper_text)
    assert records == []


@pytest.mark.unit
def test_hydrides_exceed_the_ordinary_tc_ceiling() -> None:
    """High-pressure hydrides reach 200-290 K and must not be capped.

    A flat 200 K ceiling silently discarded LaH10 (250 K), H3S (203 K) and
    every other hydride: the paper extracted fine but the record was dropped
    as "implausible", so hydride papers produced stored=0.
    """
    # Accept: real hydride Tcs.
    assert _plausible_tc("LaH10", 250.0) is True
    assert _plausible_tc("H3S", 203.0) is True
    assert _plausible_tc("CeH10", 215.0) is True
    assert _plausible_tc("YH6", 225.0) is True
    assert _plausible_tc("LaCaH11", 270.0) is True
    # Reject: beyond even the hydride ceiling.
    assert _plausible_tc("LaH10", 350.0) is False
    # Non-hydrides keep the tighter ceiling.
    assert _plausible_tc("YBa2Cu3O7", 92.0) is True
    assert _plausible_tc("YBa2Cu3O7", 400.0) is False
    assert _plausible_tc("Al", 100.0) is False


@pytest.mark.unit
def test_hydride_detection_covers_both_notations() -> None:
    """Metal hydrides end in Hn; the high-pressure ones lead with H."""
    from superconductor_db.ingest.extractor import _is_hydride

    for formula in ("LaH10", "H3S", "H3Se", "D3S", "Li2CaH16", "LaCaH11", "CeH10"):
        assert _is_hydride(formula) is True, formula
    for formula in ("YBa2Cu3O7", "MgB2", "FeSe", "La3Ni2O7", "Nb3Sn"):
        assert _is_hydride(formula) is False, formula


@pytest.mark.unit
def test_tc_gate_rejects_misattributed_values() -> None:
    """A Tc must belong to its own material, not a neighbour in the same text.

    The evidence gate proved only that a number occurred in the paper. The
    audit found it had let through ``YBa2Cu3O7 = 39 K`` (MgB2's value),
    ``FeSe = 109 K`` (the monolayer value) and computed values presented as
    measurements.
    """
    from superconductor_db.ingest.extractor import _tc_is_theirs
    from superconductor_db.schemas.common import Quantity

    paper = (
        "Bulk FeSe transitions at 8 K, whereas the monolayer film on SrTiO3 "
        "shows a sharp resistive onset at 109 K. Separately, MgB2 "
        "superconducts at 39 K. YBa2Cu3O7 has a transition near 92 K. We "
        "measured LaH10 and observed zero resistance below 250 K at 170 GPa. "
        "The calculated Tc of YBa2Cu3O7 reaches 133 K."
    )
    q = lambda v: Quantity(value=v, unit="K")  # noqa: E731

    # Kept: the paper really states these for these materials.
    assert _tc_is_theirs("FeSe", paper, q(8.0)) is True
    assert _tc_is_theirs("FeSe/Nb:SrTiO3", paper, q(109.0)) is True
    assert _tc_is_theirs("MgB2", paper, q(39.0)) is True
    assert _tc_is_theirs("YBa2Cu3O7", paper, q(92.0)) is True
    assert _tc_is_theirs("LaH10", paper, q(250.0)) is True

    # Dropped: another material's number, or a calculated value.
    assert _tc_is_theirs("YBa2Cu3O7", paper, q(39.0)) is False
    assert _tc_is_theirs("YBa2Cu3O7", paper, q(133.0)) is False
    assert _tc_is_theirs("LaH10", paper, q(288.0)) is False

    # The monolayer value is legitimately attributed to FeSe: the sentence
    # names both FeSe and its SrTiO3 substrate, and a heterostructure carries
    # one Tc. Rejecting it would discard a real measurement, which is the
    # failure mode that shrank the corpus from 500 rows to 64.
    assert _tc_is_theirs("FeSe", paper, q(109.0)) is True


@pytest.mark.unit
def test_signature_values_reject_copied_numbers() -> None:
    """A number famous for another material is evidence of a copy.

    The audit found YBa2Cu3O7 stored at 39 K and FeSe at 133 K — MgB2's and
    Hg-1223's numbers respectively. Only exact signature matches are rejected,
    so genuine results outside a textbook range (a monolayer film, a
    pressure-induced phase) still get in.
    """
    from superconductor_db.ingest.verifier import plausible_tc_range

    # Copied from another material.
    assert plausible_tc_range("YBa2Cu3O7", 39.0)[0] is False
    assert plausible_tc_range("FeSe", 133.0)[0] is False
    assert plausible_tc_range("MgB2", 92.0)[0] is False
    assert plausible_tc_range("NbSe2", 203.0)[0] is False

    # Real measurements, even outside a naive family range.
    assert plausible_tc_range("FeSe/Nb:SrTiO3", 109.0)[0] is True
    assert plausible_tc_range("SmFeAsO", 55.0)[0] is True
    assert plausible_tc_range("BaFe2As2", 39.0)[0] is True
    assert plausible_tc_range("Bi2Sr2CaCu2O8", 31.0)[0] is True
    assert plausible_tc_range("NbSe2", 3.0)[0] is True
    assert plausible_tc_range("LaH10", 250.0)[0] is True
    assert plausible_tc_range("Nb", 9.2)[0] is True


@pytest.mark.unit
def test_consensus_prefers_the_agreed_value() -> None:
    """A material's values collapse to the cluster the measurements agree on.

    Nb came back at 4.0, 9.0, 9.2 and 9.25 K. The cluster {9.0, 9.2, 9.25}
    spans 0.25 K; 4.0 stands alone five kelvin away and is an outlier, so the
    representative is 9.2 K.
    """
    from superconductor_db.ingest.consensus import decide

    # (id, composition, tc_value, notes, pressure)
    rows = [
        (1, "Nb", 4.0, None, None),
        (2, "Nb", 9.0, None, None),
        (3, "Nb", 9.2, "zero resistance", "null"),
        (4, "Nb", 9.25, None, None),
    ]
    verdict = decide(rows)
    assert verdict is not None
    assert verdict.keep[0][2] == 9.2
    assert sorted(d[2] for d in verdict.drop) == [4.0, 9.0, 9.25]


@pytest.mark.unit
def test_consensus_keeps_all_when_values_agree() -> None:
    """Distinct pressure runs of one compound are all legitimate."""
    from superconductor_db.ingest.consensus import decide

    rows = [
        (1, "LaH10", 250.0, "a", "null"),
        (2, "LaH10", 250.0, "b", "null"),
    ]
    assert decide(rows) is None


@pytest.mark.unit
def test_consensus_falls_back_to_documentation() -> None:
    """With no agreement, the better-documented row wins, not the higher one.

    Sr2RuO4 at 3.5 K is a pressure-induced transition and 1.42 K its ambient
    one; only the 1.42 K row carries evidence, and that is the value to keep.
    """
    from superconductor_db.ingest.consensus import decide

    rows = [
        (1, "Sr2RuO4", 3.5, None, None),
        (2, "Sr2RuO4", 1.42, "resistive transition", "null"),
    ]
    verdict = decide(rows)
    assert verdict is not None
    assert verdict.keep[0][2] == 1.42
    assert [d[2] for d in verdict.drop] == [3.5]


@pytest.mark.unit
def test_binding_accepts_paper_aliases() -> None:
    """A short label in prose must match the full formula in the text.

    Papers say "Bi2212" in the body and "Bi2Sr2CaCu2O8" in the abstract. The
    binding gate rejected 54 legitimate values before aliases were recognised.
    """
    from superconductor_db.ingest.verifier import formula_mentions

    assert formula_mentions("Bi2212", "the Bi2Sr2CaCu2O8 sample") is True
    assert formula_mentions("YBCO", "films of YBa2Cu3O7") is True
    assert formula_mentions("Bi2212", "MgB2 superconducts at 39 K") is False


@pytest.mark.unit
def test_binding_ignores_units_and_lab_shorthand() -> None:
    """Units must not be mistaken for rival materials.

    "303 K at 400 GPa" was read as a compound named GPa, and "RRR" as a
    formula, which rejected real values.
    """
    from superconductor_db.ingest.verifier import formula_mentions

    assert formula_mentions("YH10", "The Tc of YH10 is 303 K at 400 GPa.") is True
    assert formula_mentions("PrOs4Sb12", "PrOs4Sb12 with RRR 1000 gives 1.887 K") is True


@pytest.mark.unit
def test_specialised_prompts_are_conditional() -> None:
    """Isotope / Fermi / specific-heat prompts are sent only when warranted.

    Every dimension used to be requested unconditionally — eight model calls
    per paper. A 78-paper run spent 384 calls on papers that yielded nothing,
    while the endpoint returned 38% rate-limit errors, so retries rather than
    concurrency set the pace.
    """
    from superconductor_db.ingest.extractor import (
        _FERMI_CUES,
        _HEAT_CUES,
        _ISOTOPE_CUES,
        _mentions,
    )

    plain = "MgB2 superconducts at 39 K with an upper critical field of 14 T."
    assert _mentions(plain, _ISOTOPE_CUES) is False
    assert _mentions(plain, _FERMI_CUES) is False
    assert _mentions(plain, _HEAT_CUES) is False

    rich = (
        "We determine the isotope coefficient alpha, the Fermi temperature "
        "from de Haas-van Alphen oscillations, and the specific heat "
        "coefficient gamma."
    )
    assert _mentions(rich, _ISOTOPE_CUES) is True
    assert _mentions(rich, _FERMI_CUES) is True
    assert _mentions(rich, _HEAT_CUES) is True


@pytest.mark.unit
def test_classify_tc_evidence_marker_coverage() -> None:
    """Classification word lists cover production failure shapes.

    Both cases below were stored with a wrong/blank verdict before the
    marker lists were extended (2026-10-06): a DFT pairing was accepted as a
    measurement, and an observation headline lost its experimental flag.
    """
    from superconductor_db.ingest.extractor import classify_tc_evidence

    # DFT / electron-phonon calculation wording must read as theoretical.
    assert (
        classify_tc_evidence(
            "For strain-free 2H-Mo2N, the EPC constant (λ) and Tc are "
            "1.3 and 22.7 K, respectively."
        )
        == "theoretical"
    )
    assert (
        classify_tc_evidence(
            "Our first-principles calculations give a Tc of 22.7 K."
        )
        == "theoretical"
    )
    assert classify_tc_evidence("The McMillan Tc is 15 K.") == "theoretical"

    # Observation headlines must read as experimental.
    assert (
        classify_tc_evidence(
            "Signatures of superconductivity near 80 K in a nickelate "
            "under high pressure."
        )
        == "experimental"
    )
    assert (
        classify_tc_evidence(
            "The discovery of superconductivity in bilayer La3Ni2O7."
        )
        == "experimental"
    )

    # A sentence mixing measurement and calculation wording stays
    # experimental: measurement markers take precedence.
    assert (
        classify_tc_evidence(
            "We measured Tc = 39 K, in agreement with our DFT value of 40 K."
        )
        == "experimental"
    )
