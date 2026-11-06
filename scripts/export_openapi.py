"""Write the Phase 3 OpenAPI contract to docs."""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from delivery_recovery.api.app import app  # noqa: E402

OUT = REPO / "docs" / "delivery-recovery" / "openapi-phase3.json"
OUT.write_text(
    json.dumps(app.openapi(), indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
print(OUT)
