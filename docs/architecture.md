# Architecture

> **Scope.** This repository holds the extraction pipeline: how a
> published paper becomes a record, and what decides whether the record
> is kept. The web interface that serves the resulting database is not
> part of it.

## Overview

A pipeline that turns the superconducting literature into a structured,
source-traceable database. It collects papers, converts them to text,
asks a model for the quantities they report, and then refuses the
quantities it cannot trace back to a sentence. Everything it writes
carries the sentence it came from.

The design principle is that the pipeline should be checkable from the
outside: the evidence quote is stored with the number, so any record can
be verified by a reader, by another model, or by a script, without
re-running the extraction.

## The flow

```
  search (OpenAlex / arXiv / Crossref / WoS)
            |
            v
    PDF acquisition          scansci-pdf, direct publisher fetch,
            |                ego browser for the sites that answer
            |                a challenge instead of the file
            v
    docling -> markdown      one conversion per PDF, cached by content hash
            |
            v
    LLM extraction           one call per dimension: composition, Tc,
            |                Hc2, gap, pressure, isotopes, synthesis
            v
    the gates                verbatim evidence, material binding,
            |                physical plausibility, how the value was
            |                obtained, agreement across papers
            v
    papers + materials rows  each row keeps its evidence quote
```

The stages are separate programs, and each can be run on its own over
the cache the previous one left behind. That is deliberate: a corpus of
tens of thousands of papers is worth more when a stage can be repeated
over the papers already parsed than when the whole run has to be
restarted.

## Module layout

```
backend/src/superconductor_db/
├── core/                 settings, engine and session factory
├── db/                   SQLAlchemy models and the materials repository
│   ├── models/           material, paper, ingest run / job
│   └── repository.py     the one place that writes material rows
├── schemas/              Pydantic models for a record and its quantities
└── ingest/               the pipeline
    ├── paper_search.py       arXiv / Crossref / Semantic Scholar, in parallel
    ├── wos_search.py         Web of Science, driven through the browser
    ├── direct_fetch.py       publishers that answer a plain request
    ├── ego_fetch.py          publishers that answer a challenge
    ├── scansci_wrapper.py    the scansci-pdf subprocess
    ├── docling_parser.py     PDF -> markdown
    ├── markdown_title.py     the title docling wrote
    ├── llm_client.py         the OpenAI-compatible client
    ├── prompts.py            one prompt per dimension
    ├── extractor.py          the extraction, and the evidence gate
    ├── verifier.py           binding a number to the material it belongs to
    ├── whitelist.py          the curated material list, and the hallucination guard
    ├── families.py           the material family of a composition
    ├── consensus.py          collapsing several reported values to one
    ├── provenance.py         linking a record back to its source paper
    ├── supercon_import.py    building the SuperCon database from its CSVs
    ├── supercon_seed.py      the seed loader
    ├── paper_ingest.py       one paper, end to end
    ├── paper_search_loop.py  the discovery loop
    ├── curated_loop.py       the curated list loop
    ├── runner.py             one search query, end to end
    ├── real_runner.py        the same, against a real model
    ├── demo_runner.py        the same, against a stub
    ├── worker.py             the queue consumer
    ├── reverify.py           re-checking stored values against their papers
    ├── purge_theoretical.py  separating measurements from predictions
    ├── backfill.py           one-shot cleanup and backfill
    └── db_setup.py           the engine the loops share
```

## The gates

The extraction writes a candidate record; the gates decide whether it
becomes a row. They are written against the sentence and the material,
not against a field of a schema, so the same set applies to another
corpus.

1. **Verbatim evidence.** The value must appear in the quoted
   sentence, spelled as the paper spells it.
2. **Material binding.** The sentence that carries the value must also
   name the material.
3. **Physical plausibility.** The value must lie in the range the
   family is known to reach.
4. **How the value was obtained.** A sentence that reads as a
   calculation is treated differently from one that reads as an
   observation.
5. **Cross-paper agreement.** A compound reported by several papers
   carries several values, and none of the single-paper checks can catch
   a measurement that is the paper's own but wrong for the material.

## Data model (four tables)

| Table         | Purpose                                                                  |
|---------------|--------------------------------------------------------------------------|
| `papers`      | One row per ingested paper: DOI, title, where its text is cached.        |
| `materials`   | One row per (compound, measurement): composition, Tc, Hc2, gap, pressure, family, mechanism, and the evidence quote. |
| `ingest_runs` | One row per ingest operation; counters and status.                        |
| `ingest_jobs` | The work queue: one row per search query, claimed with `SKIP LOCKED`.     |

`alembic_version` is Alembic's own pointer. The migrations live in
`backend/alembic/versions/` and are run with `make migrate`.

The materials table is flat: a quantity is a `{value, unit}` pair in a
JSON column, because the same measurement arrives in different units
from different papers.

## Storage

The database holds the records; the files stay on disk. `PDF_CACHE_DIR`
holds the PDFs, `MARKDOWN_CACHE_DIR` the markdown, both keyed by the
content hash of the PDF, so a paper is downloaded once and parsed once
whatever happens to it afterwards. In Compose both live on the `cache`
volume.

## Verification

```bash
make up                  # postgres + the worker
make migrate             # the schema
make seed                # the SuperCon catalogue
make backend-test        # the test suite
```

See [deployment.md](deployment.md) for the wiring, and
[development.md](development.md) for the workflow.
