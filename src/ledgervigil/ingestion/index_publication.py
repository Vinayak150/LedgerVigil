"""Operator-reviewed public catalog pin; no HTTP-supplied paths or index hashes."""
from pathlib import Path
INDEX_MANIFEST_SHA256='1f7d267eae56f2d1de24a3c7831825f414dd5ac14fb8e8c7a05fbca18e3a5207'   # Filled after offline verification, before publication.
INDEX_DIRECTORY=Path(__file__).resolve().parents[3].parent/'data/public/uci-online-retail-ii/index-v1'
