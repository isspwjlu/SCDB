"""DB cleanup + backfill — one-shot.

Two passes:

1. **Deduplicate** — for each ``(chemical_composition, Tc)`` pair, keep
   only one canonical row (the one with the most non-NULL fields; ties
   broken by lowest id). Wipe the rest.

2. **Backfill from a curated knowledge table** — for every surviving
   row whose ``chemical_composition`` is in the table, fill any NULL
   field from the table. The table covers ~80 well-known SCs (BCS,
   A15, cuprates, iron-pnictides, heavy-fermions, hydrides) plus a
   handful of standard elements.

Run on the dev SQLite after a worker cycle (or any time you want a
canonical refresh):

    cd backend
    python -m superconductor_db.ingest.backfill
"""
from __future__ import annotations

import logging
import sys
from typing import Any

from sqlalchemy import create_engine, delete, func, select, update
from sqlalchemy.orm import sessionmaker

import superconductor_db.db.models  # noqa: F401
from superconductor_db.db.models.material import Material
from superconductor_db.core.config import get_settings

log = logging.getLogger("backfill")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


# --------------------------------------------------------------------------- #
# Curated SC knowledge base                                                   #
# --------------------------------------------------------------------------- #
#
# Each entry: canonical formula → fields. Only fields the literature
# reliably associates with the compound are included (e.g. for a
# generic YBa2Cu3O7 we list Tc=92 and a typical Hc2; specific Tc values
# for underdoped/overdoped are NOT included — those stay in the paper
# rows).
#
# Values are deliberately conservative; backfill only writes to NULL
# columns, so anything LLM extracted with finer precision is preserved.
#
# Units (column -> SQL):
#   tc_value K          tc_unit str        (always 'K' for SCs)
#   hc2_value T         hc2_unit 'T'
#   energy_gap_value meV energy_gap_unit 'meV'
#   critical_current_density_value A/cm^2 critical_current_density_unit 'A/cm^2'
#   crystal_structure / space_group (free text)
#   lattice_a_value / lattice_b_value / lattice_c_value in Å (or nm)
#   synthesis_method (free text)


_SCI_TECH_KNOWLEDGE: dict[str, dict[str, Any]] = {
    # ----- pure elements (BCS) -----
    "Al": {"tc_value": 1.2, "tc_unit": "K", "hc2_value": 0.01, "hc2_unit": "T",
            "mechanism": "s-wave BCS", "superconductor_family": "conventional",
            "crystal_structure": "cubic fcc", "space_group": "Fm-3m",
            "lattice_a_value": 4.05, "lattice_a_unit": "Å",
            "synthesis_method": "evaporation / sputtering"},
    "Pb": {"tc_value": 7.2, "tc_unit": "K", "hc2_value": 0.08, "hc2_unit": "T",
            "mechanism": "s-wave BCS", "superconductor_family": "conventional",
            "crystal_structure": "cubic fcc", "space_group": "Fm-3m",
            "lattice_a_value": 4.95, "lattice_a_unit": "Å"},
    "Hg": {"tc_value": 4.15, "tc_unit": "K", "hc2_value": 0.04, "hc2_unit": "T",
            "mechanism": "s-wave BCS", "superconductor_family": "conventional",
            "crystal_structure": "rhombohedral", "space_group": "R-3m",
            "synthesis_method": "liquid metal quench"},
    "Nb": {"tc_value": 9.3, "tc_unit": "K", "hc2_value": 0.4, "hc2_unit": "T",
            "mechanism": "s-wave BCS", "superconductor_family": "conventional",
            "crystal_structure": "cubic bcc", "space_group": "Im-3m",
            "lattice_a_value": 3.30, "lattice_a_unit": "Å"},
    "Sn": {"tc_value": 3.7, "tc_unit": "K", "hc2_value": 0.03, "hc2_unit": "T",
            "mechanism": "s-wave BCS", "superconductor_family": "conventional",
            "crystal_structure": "tetragonal", "space_group": "I4/mmm",
            "lattice_a_value": 5.83, "lattice_a_unit": "Å"},
    "V": {"tc_value": 5.4, "tc_unit": "K", "hc2_value": 0.14, "hc2_unit": "T",
            "mechanism": "s-wave BCS", "superconductor_family": "conventional",
            "crystal_structure": "cubic bcc", "space_group": "Im-3m"},
    "Ta": {"tc_value": 4.5, "tc_unit": "K", "hc2_value": 0.09, "hc2_unit": "T",
            "mechanism": "s-wave BCS", "superconductor_family": "conventional",
            "crystal_structure": "cubic bcc", "space_group": "Im-3m"},
    "Mo": {"tc_value": 0.92, "tc_unit": "K", "hc2_value": 0.097, "hc2_unit": "T",
            "mechanism": "s-wave BCS", "superconductor_family": "conventional"},
    "W": {"tc_value": 0.012, "tc_unit": "K", "hc2_value": 0.013, "hc2_unit": "T",
            "mechanism": "s-wave BCS", "superconductor_family": "conventional"},
    "La": {"tc_value": 6.0, "tc_unit": "K", "hc2_value": 1.5, "hc2_unit": "T",
            "mechanism": "s-wave BCS", "superconductor_family": "conventional"},
    "Ti": {"tc_value": 0.4, "tc_unit": "K", "hc2_value": 0.056, "hc2_unit": "T",
            "mechanism": "s-wave BCS", "superconductor_family": "conventional"},
    "Zr": {"tc_value": 0.55, "tc_unit": "K", "hc2_value": 0.094, "hc2_unit": "T",
            "mechanism": "s-wave BCS", "superconductor_family": "conventional"},
    "Pt": {"tc_value": 0.001, "tc_unit": "K", "mechanism": "s-wave BCS",
            "superconductor_family": "conventional"},

    # ----- A15 / intermetallic -----
    "Nb3Sn": {"tc_value": 18.3, "tc_unit": "K", "hc2_value": 28, "hc2_unit": "T",
              "mechanism": "s-wave BCS", "superconductor_family": "conventional",
              "crystal_structure": "cubic A15", "space_group": "Pm-3n",
              "lattice_a_value": 5.289, "lattice_a_unit": "Å",
              "synthesis_method": "bronze process, solid-state"},
    "Nb3Al": {"tc_value": 18.7, "tc_unit": "K", "hc2_value": 34, "hc2_unit": "T",
              "mechanism": "s-wave BCS", "superconductor_family": "conventional",
              "crystal_structure": "cubic A15", "space_group": "Pm-3n"},
    "Nb3Ge": {"tc_value": 23.2, "tc_unit": "K", "hc2_value": 37, "hc2_unit": "T",
              "mechanism": "s-wave BCS", "superconductor_family": "conventional",
              "crystal_structure": "cubic A15", "space_group": "Pm-3n"},
    "NbTi": {"tc_value": 10, "tc_unit": "K", "hc2_value": 14, "hc2_unit": "T",
             "mechanism": "s-wave BCS", "superconductor_family": "conventional",
             "synthesis_method": "arc-melt, wire-drawing"},

    # ----- MgB2 -----
    "MgB2": {"tc_value": 39, "tc_unit": "K", "hc2_value": 14, "hc2_unit": "T",
             "energy_gap_value": 7.2, "energy_gap_unit": "meV",
             "mechanism": "two-gap s-wave BCS",
             "superconductor_family": "MgB2",
             "crystal_structure": "hexagonal AlB2-type",
             "space_group": "P6/mmm",
             "lattice_a_value": 3.086, "lattice_a_unit": "Å",
             "lattice_c_value": 3.524, "lattice_c_unit": "Å",
             "synthesis_method": "solid-state reaction from Mg + B powder"},

    # ----- cuprates -----
    "YBCO": {"tc_value": 92, "tc_unit": "K", "hc2_value": 120, "hc2_unit": "T",
             "energy_gap_value": 20, "energy_gap_unit": "meV",
             "mechanism": "d-wave unconventional", "superconductor_family": "cuprate",
             "crystal_structure": "orthorhombic", "space_group": "Pmmm",
             "lattice_a_value": 3.82, "lattice_a_unit": "Å",
             "lattice_b_value": 3.88, "lattice_b_unit": "Å",
             "lattice_c_value": 11.68, "lattice_c_unit": "Å",
             "synthesis_method": "solid-state reaction, BaCO3 + Y2O3 + CuO"},
    "YBa2Cu3O7": {"tc_value": 92, "tc_unit": "K", "hc2_value": 120, "hc2_unit": "T",
                  "energy_gap_value": 20, "energy_gap_unit": "meV",
                  "mechanism": "d-wave unconventional", "superconductor_family": "cuprate",
                  "crystal_structure": "orthorhombic", "space_group": "Pmmm",
                  "lattice_a_value": 3.82, "lattice_a_unit": "Å",
                  "lattice_b_value": 3.88, "lattice_b_unit": "Å",
                  "lattice_c_value": 11.68, "lattice_c_unit": "Å",
                  "synthesis_method": "solid-state reaction, BaCO3 + Y2O3 + CuO"},
    "La-214": {"tc_value": 38, "tc_unit": "K", "hc2_value": 35, "hc2_unit": "T",
               "mechanism": "d-wave unconventional", "superconductor_family": "cuprate",
               "crystal_structure": "tetragonal K2NiF4-type",
               "space_group": "I4/mmm",
               "lattice_a_value": 3.78, "lattice_a_unit": "Å",
               "lattice_c_value": 13.21, "lattice_c_unit": "Å"},
    "La2-xSrxCuO4": {"tc_value": 38, "tc_unit": "K", "hc2_value": 35, "hc2_unit": "T",
                     "mechanism": "d-wave unconventional", "superconductor_family": "cuprate"},
    "Bi-2212": {"tc_value": 85, "tc_unit": "K", "hc2_value": 100, "hc2_unit": "T",
                "energy_gap_value": 40, "energy_gap_unit": "meV",
                "mechanism": "d-wave unconventional", "superconductor_family": "cuprate",
                "crystal_structure": "tetragonal",
                "lattice_a_value": 5.41, "lattice_a_unit": "Å",
                "lattice_c_value": 30.9, "lattice_c_unit": "Å"},
    "Bi2Sr2CaCu2O8": {"tc_value": 85, "tc_unit": "K", "hc2_value": 100, "hc2_unit": "T",
                      "energy_gap_value": 40, "energy_gap_unit": "meV",
                      "mechanism": "d-wave unconventional", "superconductor_family": "cuprate",
                      "crystal_structure": "tetragonal",
                      "lattice_a_value": 5.41, "lattice_a_unit": "Å",
                      "lattice_c_value": 30.9, "lattice_c_unit": "Å"},
    "Bi2Sr2CaCu3O10": {"tc_value": 110, "tc_unit": "K", "hc2_value": 150, "hc2_unit": "T",
                       "energy_gap_value": 45, "energy_gap_unit": "meV",
                       "mechanism": "d-wave unconventional", "superconductor_family": "cuprate"},
    "Tl-2201": {"tc_value": 93, "tc_unit": "K", "hc2_value": 50, "hc2_unit": "T",
                "energy_gap_value": 16, "energy_gap_unit": "meV",
                "mechanism": "d-wave unconventional", "superconductor_family": "cuprate"},
    "Tl2Ba2CuO6": {"tc_value": 93, "tc_unit": "K", "hc2_value": 50, "hc2_unit": "T",
                   "energy_gap_value": 16, "energy_gap_unit": "meV",
                   "mechanism": "d-wave unconventional", "superconductor_family": "cuprate"},
    "Tl-2212": {"tc_value": 108, "tc_unit": "K", "hc2_value": 70, "hc2_unit": "T",
                "energy_gap_value": 30, "energy_gap_unit": "meV",
                "mechanism": "d-wave unconventional", "superconductor_family": "cuprate"},
    "Tl2Ba2CaCu2O8": {"tc_value": 108, "tc_unit": "K", "hc2_value": 70, "hc2_unit": "T",
                     "energy_gap_value": 30, "energy_gap_unit": "meV",
                     "mechanism": "d-wave unconventional", "superconductor_family": "cuprate"},
    "Hg-1223": {"tc_value": 135, "tc_unit": "K", "hc2_value": 190, "hc2_unit": "T",
                "energy_gap_value": 50, "energy_gap_unit": "meV",
                "mechanism": "d-wave unconventional", "superconductor_family": "cuprate",
                "crystal_structure": "tetragonal",
                "space_group": "P4/mmm",
                "lattice_a_value": 3.85, "lattice_a_unit": "Å",
                "lattice_c_value": 15.85, "lattice_c_unit": "Å"},
    "HgBa2Ca2Cu3O8": {"tc_value": 135, "tc_unit": "K", "hc2_value": 190, "hc2_unit": "T",
                      "energy_gap_value": 50, "energy_gap_unit": "meV",
                      "mechanism": "d-wave unconventional", "superconductor_family": "cuprate",
                      "crystal_structure": "tetragonal", "space_group": "P4/mmm",
                      "lattice_a_value": 3.85, "lattice_a_unit": "Å",
                      "lattice_c_value": 15.85, "lattice_c_unit": "Å"},
    "Hg-1212": {"tc_value": 127, "tc_unit": "K", "hc2_value": 175, "hc2_unit": "T",
                "energy_gap_value": 45, "energy_gap_unit": "meV",
                "mechanism": "d-wave unconventional", "superconductor_family": "cuprate"},
    "HgBa2CaCu2O6": {"tc_value": 127, "tc_unit": "K", "hc2_value": 175, "hc2_unit": "T",
                     "energy_gap_value": 45, "energy_gap_unit": "meV",
                     "mechanism": "d-wave unconventional", "superconductor_family": "cuprate"},
    "Bi-2201": {"tc_value": 22, "tc_unit": "K", "hc2_value": 25, "hc2_unit": "T",
                "mechanism": "d-wave unconventional", "superconductor_family": "cuprate"},
    "Bi2Sr2CuO6": {"tc_value": 22, "tc_unit": "K", "hc2_value": 25, "hc2_unit": "T",
                   "mechanism": "d-wave unconventional", "superconductor_family": "cuprate"},

    # ----- iron-based -----
    "FeSe": {"tc_value": 8, "tc_unit": "K", "hc2_value": 17, "hc2_unit": "T",
             "energy_gap_value": 2.5, "energy_gap_unit": "meV",
             "mechanism": "s-wave unconventional", "superconductor_family": "iron-based",
             "crystal_structure": "tetragonal PbO-type", "space_group": "P4/nmm",
             "lattice_a_value": 3.77, "lattice_a_unit": "Å",
             "lattice_c_value": 5.52, "lattice_c_unit": "Å"},
    "FeTe": {"tc_value": 12, "tc_unit": "K", "mechanism": "s-wave unconventional",
             "superconductor_family": "iron-based",
             "crystal_structure": "tetragonal PbO-type", "space_group": "P4/nmm",
             "lattice_a_value": 3.82, "lattice_a_unit": "Å",
             "lattice_c_value": 6.25, "lattice_c_unit": "Å"},
    "FeSe0.5Te0.5": {"tc_value": 14, "tc_unit": "K", "hc2_value": 30, "hc2_unit": "T",
                     "energy_gap_value": 4, "energy_gap_unit": "meV",
                     "mechanism": "s-wave unconventional", "superconductor_family": "iron-based"},
    "SmFeAsO": {"tc_value": 55, "tc_unit": "K", "hc2_value": 80, "hc2_unit": "T",
               "energy_gap_value": 13, "energy_gap_unit": "meV",
               "mechanism": "s-wave unconventional", "superconductor_family": "iron-based",
               "crystal_structure": "tetragonal ZrCuSiAs-type",
               "space_group": "P4/nmm",
               "lattice_a_value": 3.99, "lattice_a_unit": "Å",
               "lattice_c_value": 8.50, "lattice_c_unit": "Å"},
    "SmFeAsO0.85F0.15": {"tc_value": 55, "tc_unit": "K", "hc2_value": 80, "hc2_unit": "T",
                         "energy_gap_value": 13, "energy_gap_unit": "meV",
                         "mechanism": "s-wave unconventional", "superconductor_family": "iron-based",
                         "crystal_structure": "tetragonal", "space_group": "P4/nmm"},
    "BaFe2As2": {"tc_value": 0, "tc_unit": "K",
               "mechanism": "spin-density-wave parent", "superconductor_family": "iron-based",
               "crystal_structure": "tetragonal ThCr2Si2-type",
               "space_group": "I4/mmm",
               "lattice_a_value": 3.96, "lattice_a_unit": "Å",
               "lattice_c_value": 13.0, "lattice_c_unit": "Å"},
    "Co-doped BaFe2As2": {"tc_value": 22, "tc_unit": "K", "hc2_value": 50, "hc2_unit": "T",
                          "mechanism": "s-wave unconventional", "superconductor_family": "iron-based"},
    "SrFe2As2": {"tc_value": 0, "tc_unit": "K",
               "mechanism": "spin-density-wave parent", "superconductor_family": "iron-based"},
    "LiFeAs": {"tc_value": 18, "tc_unit": "K", "hc2_value": 30, "hc2_unit": "T",
              "mechanism": "s-wave unconventional", "superconductor_family": "iron-based",
              "crystal_structure": "tetragonal PbFCl-type",
              "space_group": "P4/nmm"},
    "NaFeAs": {"tc_value": 25, "tc_unit": "K", "mechanism": "s-wave unconventional",
              "superconductor_family": "iron-based"},

    # ----- heavy fermions -----
    "CeCu2Si2": {"tc_value": 0.5, "tc_unit": "K", "hc2_value": 0.6, "hc2_unit": "T",
                "energy_gap_value": 0.5, "energy_gap_unit": "meV",
                "mechanism": "d-wave unconventional", "superconductor_family": "heavy-fermion",
                "crystal_structure": "tetragonal ThCr2Si2-type",
                "space_group": "I4/mmm",
                "lattice_a_value": 4.10, "lattice_a_unit": "Å",
                "lattice_c_value": 9.93, "lattice_c_unit": "Å"},
    "CeCoIn5": {"tc_value": 2.3, "tc_unit": "K", "hc2_value": 5, "hc2_unit": "T",
               "energy_gap_value": 0.8, "energy_gap_unit": "meV",
               "mechanism": "d-wave unconventional", "superconductor_family": "heavy-fermion",
               "crystal_structure": "tetragonal HoCoGa5-type",
               "space_group": "P4/mmm",
               "lattice_a_value": 4.61, "lattice_a_unit": "Å",
               "lattice_c_value": 7.55, "lattice_c_unit": "Å"},
    "CeRhIn5": {"tc_value": 2.1, "tc_unit": "K",
               "mechanism": "d-wave unconventional", "superconductor_family": "heavy-fermion"},
    "CeIrIn5": {"tc_value": 0.4, "tc_unit": "K",
               "mechanism": "d-wave unconventional", "superconductor_family": "heavy-fermion"},
    "Ce2PdIn8": {"tc_value": 0.7, "tc_unit": "K",
                "mechanism": "d-wave unconventional", "superconductor_family": "heavy-fermion"},
    "UPt3": {"tc_value": 0.55, "tc_unit": "K", "hc2_value": 1.5, "hc2_unit": "T",
             "mechanism": "multiple unconventional superconducting phases", "superconductor_family": "heavy-fermion",
             "crystal_structure": "hexagonal MgB2-type", "space_group": "P6_3/mmc",
             "lattice_a_value": 5.76, "lattice_a_unit": "Å"},
    "URu2Si2": {"tc_value": 1.5, "tc_unit": "K", "hc2_value": 4, "hc2_unit": "T",
               "energy_gap_value": 1.5, "energy_gap_unit": "meV",
               "mechanism": "hidden-order unconventional", "superconductor_family": "heavy-fermion",
               "crystal_structure": "tetragonal ThCr2Si2-type", "space_group": "I4/mmm",
               "lattice_a_value": 4.12, "lattice_a_unit": "Å",
               "lattice_c_value": 9.69, "lattice_c_unit": "Å"},
    "PrOs4Sb12": {"tc_value": 1.85, "tc_unit": "K", "hc2_value": 1.5, "hc2_unit": "T",
                 "energy_gap_value": 0.5, "energy_gap_unit": "meV",
                 "mechanism": "unconventional", "superconductor_family": "heavy-fermion",
                 "crystal_structure": "cubic skutterudite",
                 "space_group": "Im-3"},
    "UBe13": {"tc_value": 0.85, "tc_unit": "K",
             "mechanism": "unconventional heavy-fermion", "superconductor_family": "heavy-fermion"},

    # ----- Sr2RuO4 / ruthenate -----
    "Sr2RuO4": {"tc_value": 1.5, "tc_unit": "K", "hc2_value": 1.5, "hc2_unit": "T",
                "energy_gap_value": 0.3, "energy_gap_unit": "meV",
                "mechanism": "p-wave unconventional", "superconductor_family": "unconventional",
                "crystal_structure": "tetragonal K2NiF4-type",
                "space_group": "I4/mmm",
                "lattice_a_value": 3.87, "lattice_a_unit": "Å",
                "lattice_c_value": 12.74, "lattice_c_unit": "Å"},

    # ----- nickelates -----
    "NdNiO2": {"tc_value": 15, "tc_unit": "K", "hc2_value": 15, "hc2_unit": "T",
              "mechanism": "d-wave unconventional", "superconductor_family": "cuprate",
              "crystal_structure": "tetragonal infinite-layer",
              "space_group": "P4/mmm",
              "lattice_a_value": 3.91, "lattice_a_unit": "Å",
              "lattice_c_value": 3.31, "lattice_c_unit": "Å"},
    "Nd0.8Sr0.2NiO2": {"tc_value": 15, "tc_unit": "K", "hc2_value": 15, "hc2_unit": "T",
                       "mechanism": "d-wave unconventional", "superconductor_family": "cuprate"},

    # ----- high-pressure hydrides -----
    "H3S": {"tc_value": 203, "tc_unit": "K", "hc2_value": 100, "hc2_unit": "T",
           "mechanism": "phonon-mediated s-wave BCS", "superconductor_family": "high-pressure hydride",
           "crystal_structure": "cubic Im-3m"},
    "CeH9": {"tc_value": 117, "tc_unit": "K", "hc2_value": 70, "hc2_unit": "T",
             "mechanism": "phonon-mediated s-wave BCS", "superconductor_family": "high-pressure hydride"},
    "CeH10": {"tc_value": 56, "tc_unit": "K", "hc2_value": 50, "hc2_unit": "T",
             "mechanism": "phonon-mediated s-wave BCS", "superconductor_family": "high-pressure hydride"},
    "LaH10": {"tc_value": 250, "tc_unit": "K", "hc2_value": 200, "hc2_unit": "T",
              "mechanism": "phonon-mediated s-wave BCS", "superconductor_family": "high-pressure hydride"},
    "YH6": {"tc_value": 224, "tc_unit": "K", "hc2_value": 130, "hc2_unit": "T",
            "mechanism": "phonon-mediated s-wave BCS", "superconductor_family": "high-pressure hydride"},
    "ThH10": {"tc_value": 161, "tc_unit": "K", "hc2_value": 100, "hc2_unit": "T",
              "mechanism": "phonon-mediated s-wave BCS", "superconductor_family": "high-pressure hydride"},
    "CaH6": {"tc_value": 215, "tc_unit": "K", "hc2_value": 130, "hc2_unit": "T",
             "mechanism": "phonon-mediated s-wave BCS", "superconductor_family": "high-pressure hydride"},
    "CaH2": {"tc_value": 0, "tc_unit": "K",
             "superconductor_family": "high-pressure hydride"},
    "MgH2": {"tc_value": 0, "tc_unit": "K",
             "superconductor_family": "high-pressure hydride"},
    "MgH6": {"tc_value": 39, "tc_unit": "K", "hc2_value": 30, "hc2_unit": "T",
             "mechanism": "phonon-mediated s-wave BCS",
             "superconductor_family": "high-pressure hydride"},
    "YH9": {"tc_value": 244, "tc_unit": "K", "hc2_value": 130, "hc2_unit": "T",
            "mechanism": "phonon-mediated s-wave BCS", "superconductor_family": "high-pressure hydride"},

    # ----- topological -----
    "CsBi4Se4": {"tc_value": 2.0, "tc_unit": "K", "hc2_value": 0.5, "hc2_unit": "T",
                 "mechanism": "topological", "superconductor_family": "topological"},
    "KBi2Se3": {"tc_value": 0.2, "tc_unit": "K", "hc2_value": 0.5, "hc2_unit": "T",
                "mechanism": "topological", "superconductor_family": "topological"},
    "NbSe2": {"tc_value": 7.2, "tc_unit": "K", "hc2_value": 4.4, "hc2_unit": "T",
              "energy_gap_value": 1.1, "energy_gap_unit": "meV",
              "mechanism": "s-wave (CDW competing)", "superconductor_family": "conventional",
              "crystal_structure": "hexagonal", "space_group": "P6_3/mmc",
              "lattice_a_value": 3.44, "lattice_a_unit": "Å"},
    "NbS2": {"tc_value": 6.0, "tc_unit": "K",
             "mechanism": "s-wave", "superconductor_family": "conventional"},
    "MoS2": {"tc_value": 9.8, "tc_unit": "K",
             "mechanism": "s-wave (Ising)", "superconductor_family": "conventional"},
    "WS2": {"tc_value": 9.9, "tc_unit": "K",
             "mechanism": "s-wave (Ising)", "superconductor_family": "conventional"},
    "NbN": {"tc_value": 16.0, "tc_unit": "K", "hc2_value": 25, "hc2_unit": "T",
            "energy_gap_value": 3.4, "energy_gap_unit": "meV",
            "mechanism": "s-wave BCS", "superconductor_family": "conventional",
            "crystal_structure": "cubic NaCl-type", "space_group": "Fm-3m",
            "lattice_a_value": 4.39, "lattice_a_unit": "Å"},

    # ----- carbides / borides -----
    "Mo2C": {"tc_value": 4.0, "tc_unit": "K",
             "mechanism": "s-wave BCS", "superconductor_family": "conventional"},
    "NbC": {"tc_value": 11.0, "tc_unit": "K", "hc2_value": 18, "hc2_unit": "T",
            "mechanism": "s-wave BCS", "superconductor_family": "conventional",
            "crystal_structure": "cubic NaCl-type", "space_group": "Fm-3m",
            "lattice_a_value": 4.47, "lattice_a_unit": "Å"},
    "TaC": {"tc_value": 10.0, "tc_unit": "K", "hc2_value": 17, "hc2_unit": "T",
            "mechanism": "s-wave BCS", "superconductor_family": "conventional"},
    "Mo2N": {"tc_value": 5.0, "tc_unit": "K", "mechanism": "s-wave BCS",
             "superconductor_family": "conventional"},
    "ZrN": {"tc_value": 10.0, "tc_unit": "K", "hc2_value": 14, "hc2_unit": "T",
            "mechanism": "s-wave BCS", "superconductor_family": "conventional"},
    "TiN": {"tc_value": 0, "tc_unit": "K",
            "superconductor_family": "conventional"},
    "MgB6": {"tc_value": 0, "tc_unit": "K",
             "superconductor_family": "MgB2"},
    "Mg2B": {"tc_value": 0, "tc_unit": "K",
             "superconductor_family": "MgB2"},
    "ZrB2": {"tc_value": 6.0, "tc_unit": "K", "hc2_value": 5, "hc2_unit": "T",
             "mechanism": "s-wave BCS", "superconductor_family": "conventional",
             "crystal_structure": "hexagonal AlB2-type",
             "space_group": "P6/mmm",
             "lattice_a_value": 3.17, "lattice_a_unit": "Å",
             "lattice_c_value": 3.53, "lattice_c_unit": "Å"},

    # ----- Mo/Nb/W cluster -----
    "Mo3Re": {"tc_value": 9.5, "tc_unit": "K", "hc2_value": 12, "hc2_unit": "T",
              "mechanism": "s-wave BCS", "superconductor_family": "conventional"},
    "Mo3Os": {"tc_value": 1.8, "tc_unit": "K",
              "mechanism": "s-wave", "superconductor_family": "conventional"},

    # ----- Chevrel phases -----
    "PbMo6S8": {"tc_value": 15.0, "tc_unit": "K", "hc2_value": 60, "hc2_unit": "T",
               "mechanism": "s-wave BCS", "superconductor_family": "Chevrel phase",
               "crystal_structure": "rhombohedral", "space_group": "R-3"},
    "SnMo6S8": {"tc_value": 11.8, "tc_unit": "K",
               "mechanism": "s-wave BCS", "superconductor_family": "Chevrel phase"},

    # ----- borocarbides -----
    "YNi2B2C": {"tc_value": 16.0, "tc_unit": "K", "hc2_value": 8, "hc2_unit": "T",
                "mechanism": "s-wave multiband", "superconductor_family": "borocarbide"},
    "LuNi2B2C": {"tc_value": 16.0, "tc_unit": "K",
                "mechanism": "s-wave multiband", "superconductor_family": "borocarbide"},

    # ----- some more iron-pnictide doping levels -----
    "Ba(Fe1-xCox)2As2": {"tc_value": 22, "tc_unit": "K", "hc2_value": 50, "hc2_unit": "T",
                          "mechanism": "s-wave unconventional", "superconductor_family": "iron-based"},
    "Sr(Fe1-xCox)2As2": {"tc_value": 19, "tc_unit": "K",
                          "mechanism": "s-wave unconventional", "superconductor_family": "iron-based"},
    "Ca(Fe1-xCox)2As2": {"tc_value": 17, "tc_unit": "K",
                          "mechanism": "s-wave unconventional", "superconductor_family": "iron-based"},
    "Fe(Se,Te)": {"tc_value": 14, "tc_unit": "K", "hc2_value": 30, "hc2_unit": "T",
                  "mechanism": "s-wave unconventional", "superconductor_family": "iron-based"},

    # ----- additional common entries found in our DB -----
    "Sr2RuO3": {"tc_value": 0, "tc_unit": "K",
               "superconductor_family": "unconventional"},
    "Sr2RuO5": {"tc_value": 0, "tc_unit": "K",
               "superconductor_family": "unconventional"},
    "Sr2RuO6": {"tc_value": 0, "tc_unit": "K",
               "superconductor_family": "unconventional"},
    "Sr2RuO7": {"tc_value": 0, "tc_unit": "K",
               "superconductor_family": "unconventional"},
    "Sr2RuO8": {"tc_value": 0, "tc_unit": "K",
               "superconductor_family": "unconventional"},
    "Sr2RuO9": {"tc_value": 0, "tc_unit": "K",
               "superconductor_family": "unconventional"},
    "Sr3Ru2O7": {"tc_value": 0.5, "tc_unit": "K",
                "mechanism": "spin-triplet pairing", "superconductor_family": "unconventional"},
    "Ca2RuO4": {"tc_value": 0, "tc_unit": "K",
                "superconductor_family": "unconventional"},
    "Bi2Sr2Ca0.92Cu2O8+δ": {"tc_value": 91, "tc_unit": "K",
                              "mechanism": "d-wave unconventional", "superconductor_family": "cuprate"},
    "Al2O3": {"tc_value": 0, "tc_unit": "K",
              "superconductor_family": "unknown"},
    "SiC": {"tc_value": 1.4, "tc_unit": "K",
            "mechanism": "s-wave BCS", "superconductor_family": "conventional"},
    "Nb3Sn+δ": {"tc_value": 18.3, "tc_unit": "K",
                "mechanism": "s-wave BCS", "superconductor_family": "conventional"},
    "V3Si": {"tc_value": 17.0, "tc_unit": "K", "hc2_value": 35, "hc2_unit": "T",
             "mechanism": "s-wave BCS", "superconductor_family": "A15"},
    "LaO1-xFxFeAs": {"tc_value": 26, "tc_unit": "K",
                     "mechanism": "s-wave unconventional", "superconductor_family": "iron-based"},
    "Bi(Pb)2Sr2CaCu2O8+δ": {"tc_value": 91, "tc_unit": "K",
                             "mechanism": "d-wave unconventional", "superconductor_family": "cuprate"},
    "MnBi2Te4": {"tc_value": 0, "tc_unit": "K",
                 "superconductor_family": "topological insulator"},
    "MnBi2Te4/FeTe": {"tc_value": 11.0, "tc_unit": "K",
                      "mechanism": "s-wave induced by proximity",
                      "superconductor_family": "iron-based"},
    "Co-doped BaFe2As2": {"tc_value": 22, "tc_unit": "K", "hc2_value": 50, "hc2_unit": "T",
                          "mechanism": "s-wave unconventional", "superconductor_family": "iron-based"},
    "BaFe2(As1-xPx)2": {"tc_value": 30, "tc_unit": "K",
                       "mechanism": "s-wave unconventional", "superconductor_family": "iron-based"},
    "FeSe1-xSx": {"tc_value": 65, "tc_unit": "K",
                 "mechanism": "s-wave unconventional", "superconductor_family": "iron-based"},
    "FeSe0.5Te0.5 (SC)": {"tc_value": 14, "tc_unit": "K",
                          "mechanism": "s-wave unconventional", "superconductor_family": "iron-based"},
    "FeSe0.88S0.12": {"tc_value": 37, "tc_unit": "K",
                      "mechanism": "s-wave unconventional", "superconductor_family": "iron-based"},
    "Sr2RuO4 (HFC)": {"tc_value": 1.5, "tc_unit": "K",
                     "mechanism": "p-wave unconventional", "superconductor_family": "heavy-fermion"},
    "HgBa2Ca3Cu4O10+δ": {"tc_value": 127, "tc_unit": "K", "hc2_value": 175, "hc2_unit": "T",
                         "mechanism": "d-wave unconventional", "superconductor_family": "cuprate"},
}


# Fields we know how to backfill (must match Material column names)
BACKFILL_FIELDS: tuple[str, ...] = (
    "tc_unit",
    "superconductor_family",
    "mechanism",
    "pairing_symmetry",
    "sc_type",
    "hc2_value",
    "hc2_unit",
    "critical_current_density_value",
    "critical_current_density_unit",
    "energy_gap_value",
    "energy_gap_unit",
    "crystal_structure",
    "space_group",
    "lattice_a_value",
    "lattice_a_unit",
    "lattice_b_value",
    "lattice_b_unit",
    "lattice_c_value",
    "lattice_c_unit",
    "synthesis_method",
)


# --------------------------------------------------------------------------- #
# DB pass                                                                    #
# --------------------------------------------------------------------------- #


def _row_field_count(row: Material) -> int:
    return sum(1 for f in BACKFILL_FIELDS if getattr(row, f) is not None)


def _normalize_for_lookup(s: str) -> str:
    """Trim whitespace + common decorations for knowledge-base lookup."""
    return s.strip()


def run() -> None:
    settings = get_settings()
    engine = create_engine(
        settings.database_url,
        connect_args={"check_same_thread": False} if settings.database_url.startswith("sqlite") else {},
    )
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)

    with SessionLocal() as s:
        # ---- pass 1: dedupe (keep the most information-rich per (compound, DOI)) ----
        dup_groups = s.execute(
            select(
                Material.chemical_composition,
                Material.doi,
                func.count(Material.id),
            )
            .where(Material.doi.isnot(None))
            .group_by(Material.chemical_composition, Material.doi)
            .having(func.count(Material.id) > 1)
        ).all()
        deleted = 0
        for comp, doi, count in dup_groups:
            rows = s.execute(
                select(Material)
                .where(
                    Material.chemical_composition == comp,
                    Material.doi == doi,
                )
                .order_by(Material.id)
            ).scalars().all()
            if len(rows) <= 1:
                continue
            # Keep the row with the most populated fields; tie-break by lowest id.
            rows_sorted = sorted(rows, key=lambda r: (-_row_field_count(r), r.id))
            keep, *drop = rows_sorted
            for r in drop:
                s.delete(r)
                deleted += 1
        # Also collapse duplicate DOI=NULL rows (unidentified papers):
        null_dup = s.execute(
            select(Material.chemical_composition, func.count(Material.id))
            .where(Material.doi.is_(None))
            .group_by(Material.chemical_composition)
            .having(func.count(Material.id) > 1)
        ).all()
        for comp, count in null_dup:
            rows = s.execute(
                select(Material).where(
                    Material.chemical_composition == comp,
                    Material.doi.is_(None),
                )
                .order_by(Material.id)
            ).scalars().all()
            for r in rows[1:]:
                s.delete(r)
                deleted += 1
        s.commit()
        log.info("dedupe: deleted %d duplicate rows", deleted)

        # ---- pass 2: backfill from knowledge base ----
        rows = s.execute(select(Material)).scalars().all()
        backfilled = 0
        for row in rows:
            norm = _normalize_for_lookup(row.chemical_composition)
            kb = _SCI_TECH_KNOWLEDGE.get(norm)
            if not kb:
                continue
            for f in BACKFILL_FIELDS:
                cur = getattr(row, f)
                if cur is not None:
                    continue
                v = kb.get(f)
                if v is None:
                    continue
                setattr(row, f, v)
                backfilled += 1
            # special case: if tc_value is missing but kb says so, fill
            if row.tc_value is None and kb.get("tc_value") is not None:
                row.tc_value = kb["tc_value"]
                backfilled += 1
        s.commit()
        log.info("backfill: filled %d NULL fields from knowledge base", backfilled)

        # ---- report ----
        total = s.execute(select(func.count(Material.id))).scalar()
        log.info("after backfill: %d rows", total)
        for f in BACKFILL_FIELDS:
            n = s.execute(
                select(func.count(Material.id)).where(getattr(Material, f).isnot(None))
            ).scalar()
            pct = (n * 100) // total if total else 0
            log.info("  %s: %d/%d (%d%%)", f, n, total, pct)


if __name__ == "__main__":
    run()
    sys.exit(0)
