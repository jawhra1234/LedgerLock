"""Serialisation.

The three sources go to data/raw/. The two ground-truth files and the manifest
go to data/truth/. The pipeline package is only ever allowed to read from
data/raw/ -- see io.loaders, which enforces it.
"""

from __future__ import annotations

import csv
import json
from datetime import date, datetime
from enum import Enum
from pathlib import Path

from pydantic import BaseModel

from .. import config
from ..domain.taxonomy import EXCEPTION_META, ExceptionCode
from .engine import World
from .params import RATE_BASIS

RAW_FILES = {
    "orders": "orders.csv",
    "pg_entries": "pg_entries.csv",
    "bank_statement": "bank_statement.csv",
}
TRUTH_FILES = {
    "links": "truth_links.csv",
    "exceptions": "truth_exceptions.csv",
}


def _cell(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, Enum):
        return str(value.value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def _write_csv(path: Path, rows: list[BaseModel]) -> int:
    if not rows:
        path.write_text("", encoding="utf-8")
        return 0
    cols = list(type(rows[0]).model_fields)
    with path.open("w", newline="", encoding="utf-8") as fh:
        # LF explicitly, not the csv module's default CRLF. These files are
        # committed and compared byte-for-byte, so a platform-dependent line
        # ending makes the reproducibility claim true only on the machine that
        # generated them -- which is exactly what happened. See F15.
        wr = csv.writer(fh, lineterminator="\n")
        wr.writerow(cols)
        for r in rows:
            wr.writerow([_cell(getattr(r, c)) for c in cols])
    return len(rows)


def _manifest(w: World, counts: dict[str, int]) -> dict:
    by_code: dict[str, dict] = {}
    for code in ExceptionCode:
        meta = EXCEPTION_META[code]
        n = sum(1 for x in w.exceptions if x.code is code)
        by_code[code.value] = {
            "label": meta.label,
            "resolvability": meta.resolvability.value,
            "expected_tier": meta.expected_tier.value,
            # Recorded so a reader can see which population each rate was
            # measured against, not just how many landed.
            "rate": w.spec.rates.get(code, 0.0),
            "basis": RATE_BASIS[code].value,
            "injected": n,
        }
    spec = w.spec
    return {
        "profile": spec.name,
        "seed": spec.seed,
        # Any number published anywhere in this project cites this manifest, so
        # a reader can regenerate the exact dataset it was measured on.
        "reproduce": f"python -m ledgerlock generate --profile {spec.name} --seed {spec.seed}",
        "spec": {
            "n_orders": spec.n_orders,
            "n_days": spec.n_days,
            "refund_rate": spec.refund_rate,
            "chargeback_rate": spec.chargeback_rate,
            "order_fail_rate": spec.order_fail_rate,
        },
        "config": {
            "mdr_rates": {k: str(v) for k, v in config.MDR_RATES.items()},
            "gst_rate": str(config.GST_RATE),
            "tds_rate": str(config.TDS_RATE),
            "chargeback_fee_paise": config.CHARGEBACK_FEE_PAISE,
            "refund_returns_mdr": config.REFUND_RETURNS_MDR,
            "rolling_reserve_pct": str(config.ROLLING_RESERVE_PCT),
            "reserve_release_days": config.RESERVE_RELEASE_DAYS,
            "settlement_cycle_days": config.SETTLEMENT_CYCLE_DAYS,
            "rounding_tolerance_paise": config.ROUNDING_TOLERANCE_PAISE,
            "world_start": config.WORLD_START.isoformat(),
        },
        "counts": counts,
        "settlements": len(w.settlements),
        # Proven on the pre-injection world: every settlement satisfied
        # sum(member nets) == payout == bank credit before any corruption.
        "clean_identity_verified": True,
        "exceptions_by_code": by_code,
    }


def write_world(w: World, root: Path) -> dict:
    raw, truth = root / "raw", root / "truth"
    raw.mkdir(parents=True, exist_ok=True)
    truth.mkdir(parents=True, exist_ok=True)

    counts = {
        "orders": _write_csv(raw / RAW_FILES["orders"], w.orders),
        "pg_entries": _write_csv(raw / RAW_FILES["pg_entries"], w.entries),
        "bank_lines": _write_csv(raw / RAW_FILES["bank_statement"], w.bank_lines),
        "truth_links": _write_csv(truth / TRUTH_FILES["links"], w.links),
        "truth_exceptions": _write_csv(truth / TRUTH_FILES["exceptions"], w.exceptions),
    }
    manifest = _manifest(w, counts)
    (truth / "manifest.json").write_text(
        json.dumps(manifest, indent=2, default=str), encoding="utf-8",
        newline="\n")
    return manifest
