# Notices

## Ownership and licence

This repository's source is published by its owner. **No licence file has been added**, so no licence is granted by this
repository yet; the owner should choose one before inviting reuse. Nothing here asserts rights beyond those the owner holds.

## Third-party data

- **Online Retail II** (UCI Machine Learning Repository), Chen, D. (2012), https://doi.org/10.24432/C5CG6D, licensed
  CC BY 4.0. Used only as public observational data for compatibility tests (`docs/datasets/UCI_ONLINE_RETAIL_II.json`,
  `tests/test_plan_inspection.py`). The workbook and the derived index are **not** stored in this repository; CI obtains the
  index only from an operator-provided artifact URL (`LV_PUBLIC_INDEX_URL`). It is not settlement ground truth and not
  reviewed accounting data.

## Third-party software

Python and JavaScript dependencies are listed with exact versions in `requirements.lock`, `worker-requirements.lock` and
`web/package-lock.json`; each keeps its own licence.

## Data in this repository

All fixtures, evaluation cases and examples are SYNTHETIC unless labelled as the public dataset above. No customer data,
QuickBooks data, credentials, backups or database dumps are included; `tools/secret_scan.py` runs in CI to keep it that way.
