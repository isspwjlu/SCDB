# SCDB — Superconducting Database: superconducting material extraction pipeline

The LLM-assisted pipeline that turns a corpus of superconducting papers into a
structured, source-traceable database of materials and their measured
properties.

Every record it writes carries the sentence it came from, the DOI of the paper
it came from, and a gate that refuses the record when the evidence does not
support it. The point of the pipeline is not the numbers — those are in the
papers — it is that each number can be walked back to the page it was read
from.

> The web interface built on top of this pipeline is **not** part of this
> repository. What is here is the extraction itself: the parsers, the
> prompts, the validators, the database model, and the tools that drive them.

## The paper

The pipeline, the gates and the audit are described in

> **Auditing a superconducting database built by a large language model**
>
> Wenjian Lu
>
> arXiv:2611.29445 [cond-mat.supr-con; cond-mat.mtrl-sci] —
> <https://arxiv.org/abs/2611.29445>

It reports what the gates reject and why, audits 500 records sampled in
proportion to the corpus with two independent models, and gives the two
rates that matter for a database built this way: how often a stored value
can be traced to the sentence it was read from (93.4 % of the audited
records), and how the values divide between measurements and predictions
(91.8 % are measurements).

If you use the pipeline or the database, please cite it:

```bibtex
@misc{lu2026auditing,
  title        = {Auditing a superconducting database built by a large language model},
  author       = {Lu, Wenjian},
  year         = {2026},
  eprint       = {2611.29445},
  archivePrefix= {arXiv},
  primaryClass = {cond-mat.supr-con},
  doi          = {10.48550/arXiv.2611.29445}
}
```

## How it works

```
PDF ──► docling ──► markdown ──► LLM extraction ──► evidence gate ──► SQLite/Postgres
              │                        │                    │
        cache/markdown/         one call per            a record whose
        cache/pdfs/             dimension               evidence does not
                               (composition, Tc,        not support it is
                               Hc2, gap, pressure,      not written at all
                               synthesis)
```

1. **Parsing.** `docling` turns each PDF into markdown, cached by the content
   hash of the file. A paper is parsed once, whatever happens to it after.
2. **Extraction.** One LLM call per dimension — composition, critical
   temperature, upper critical field, energy gap, pressure, synthesis — so a
   bad answer to one question cannot spoil the others.
3. **The gate.** The extraction has to quote the passage that supports each
   value, and the passage has to contain it. A value the text does not
   support is dropped rather than stored, which is why the corpus is smaller
   than the number of papers but every row in it is worth having.

## What comes out

| Table       | Holds                                                           |
|-------------|-----------------------------------------------------------------|
| `papers`    | one row per ingested paper: DOI, title, where its text is cached |
| `materials` | one row per (compound, measurement): composition, Tc, Hc2, gap, pressure, family, mechanism, and the evidence quote |

A compound measured at one temperature is one measurement, however many papers
report it. `backend/scripts/cleanup_duplicates.py` merges the rows that
disagree about the *sample* into `meta['variants']` and keeps every source
DOI in `meta['source_dois']`, so the merge loses nothing.

## Running it

The pipeline needs a database; Docker Compose brings it up, and the worker
runs inside it.

```bash
cp .env.example .env       # then point it at your model endpoint
make up                    # postgres + the worker
make migrate               # the schema
make seed                  # the SuperCon catalogue
make backend-test          # the test suite
```

The extraction itself is driven by the scripts in `backend/tools/`:

| Script                                 | What it does                                        |
|----------------------------------------|-----------------------------------------------------|
| `download_papers.py`                   | fetch the papers by DOI into the PDF cache          |
| `docling_worker.py`                    | parse the PDFs into markdown                        |
| `run_extraction.py`                    | run the LLM extraction over the parsed corpus       |
| `cleanup_duplicates.py`                | merge the rows that describe the same measurement   |
| `audit_ledger.py`, `audit_stats_500.py` | the evidence-gate and audit statistics            |
| `gate_stats.py`, `master_usage.py`     | what the gates rejected, and what the run cost      |
| `make_audit_sample.py`, `freeze_audit_500.py` | draw the audit sample and freeze the verdicts |
| `audit_with_model.py`                  | re-read the records with a second, independent model |
| `audit_comparison.py`                  | what the two judges said about the same records     |
| `check_paper_numbers.py`               | check every published number against the file it comes from |
| `check_audit_claims.py`, `check_corpus_claims.py` | the same, for the audit and the corpus   |

Every one of them takes `--help`.

## The LLM

Any OpenAI-compatible endpoint works. The default configuration points at a
local model; the pipeline that produced the published corpus ran against
`MiniMax-M3.1-Flash`, which always reasons, so the extraction runs with
`LLM_REASONING_EFFORT=low` and a token budget that covers the thinking.

`LLM_MAX_CONCURRENCY` defaults to 1. The calls are serial on purpose: a
concurrent burst is what makes a rate-limited endpoint answer with 429, and a
paper that failed is a paper that was not extracted.

## Layout

```
backend/      the package: ingest/, db/, core/, plus the tools and tests
docs/         architecture, development and deployment notes
```

The datasets are not in this repository.  The SuperCon catalogue is
distributed by NIMS for research use, and the corpus records are the
database itself; both are obtained with the scripts below rather than
committed here.

## Data

The seed catalogue is the **NIMS SuperCon database**, which the National
Institute for Materials Science publishes for research use.  Fetch it with

```bash
python backend/scripts/download_supercon.py --out seeds/supercon.csv
```

and point the importer at the CSV:

```bash
python -m superconductor_db.ingest.supercon_seed --path seeds/supercon.csv
```

The nickelate pressure table is a workbook that is not redistributed here
either; it goes in the directory `SUPERCON_CSV_DIR` names, and the importer
takes it from there.  Everything the extraction adds — the papers, the
markdown, the extracted records — is derived from the literature and
carries its own DOI.

## Licence

Copyright 2026 Institute of Solid State Physics, Chinese Academy of Sciences.

Apache License 2.0 — see [LICENSE](LICENSE).
