"""Synthetic processor-statement ingestion (Stage 2A §19.2 path A). Internal only: no HTTP endpoint.

`statement_format` is pure parsing and validation. `store` records the attempt through the restricted
`lv_ingest` login and never writes financial tables.
"""
