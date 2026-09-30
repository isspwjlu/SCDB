"""Ingestion package.

Modules here implement:

- :mod:`superconductor_db.ingest.supercon_seed` -- one-shot SuperCon CSV loader.
- :mod:`superconductor_db.ingest.llm_client` (Task 25) -- OpenAI-compatible chat.
- :mod:`superconductor_db.ingest.extractor` (Task 26) -- 8-prompt LLM extractor.
- :mod:`superconductor_db.ingest.scansci_wrapper` (Task 27) -- scansci-pdf subprocess.
- :mod:`superconductor_db.ingest.paper_search` (Task 28) -- arXiv/CrossRef/S2.
- :mod:`superconductor_db.ingest.worker` + :mod:`runner` (Task 29) -- queue worker.
"""