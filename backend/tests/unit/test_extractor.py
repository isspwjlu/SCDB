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
    """Build an LLMClient whose ``extract_json`` returns ``responses[i]`` on call ``i``."""
    c = LLMClient(settings=None) if False else object.__new__(LLMClient)
    c.settings = None  # not used by extract_json path
    queue = list(responses)

    async def fake_extract_json(messages):  # noqa: ANN001 - signature
        if not queue:
            raise AssertionError("LLM called more times than test provided")
        return queue.pop(0)

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
    """End-to-end: given a fixed set of LLM payloads, returns one record."""
    paper_text = "YBa2Cu3O7 exhibits superconductivity at 92 K."

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
                    "Tc": {"value": 92, "unit": "K"},
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

    client = _client(responses)
    extractor = SuperconductingExtractor(client)
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
    dimension_payloads = {
        "base_sc_props": [{"MgB2": {"Tc": {"value": 39, "unit": "K"}}}],
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
    records = await extractor.extract_all("MgB2 paper")
    assert len(records) == 1
    assert records[0].material_chemical_composition == "MgB2"
    # Tc survived (base_sc_props succeeded).
    assert records[0].superconducting_property.Tc.value == 39.0
    # Inclusion gate passed (Tc is real).
    assert records[0].has_meaningful_data() is True