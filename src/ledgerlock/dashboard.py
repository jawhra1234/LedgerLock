"""Data preparation for the dashboard.

Everything here is a pure function over two artefacts on disk: `recon.json`
(what the pipeline decided) and `score.json` (how it scored). **There is no
reconciliation logic in this module and no access to ground truth**, which is
the point -- a viewer that recomputed anything would be a second implementation
free to disagree with the one that was actually measured, and the number on the
screen would stop being the number in the report.

Kept separate from the Streamlit app so it can be tested without a browser.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

# Overridable so the app can be pointed at another run's artefacts, and so a
# test can render the whole page against a world it built itself.
DATA_OUT = Path(os.getenv("LEDGERLOCK_OUT", str(Path("data") / "out")))

ACTION_ORDER = ("escalated", "deferred", "out_of_scope",
                "auto_resolved", "explained")

ACTION_LABEL = {
    "escalated": "needs a human",
    "deferred": "nothing wrong, revisit next cycle",
    "out_of_scope": "real, but not gateway money",
    "auto_resolved": "closed automatically, reported anyway",
    "explained": "accounted for by other findings",
}


def _out_dir() -> Path:
    """Resolved per call, so setting LEDGERLOCK_OUT after import still works."""
    return Path(os.getenv("LEDGERLOCK_OUT", str(Path("data") / "out")))


class MissingArtifacts(FileNotFoundError):
    """Raised when the dashboard is opened before anything has been run."""


@dataclass
class Board:
    recon: dict
    score: dict

    # -- headline ----------------------------------------------------------
    @property
    def profile(self) -> str:
        return self.score.get("profile") or "?"

    @property
    def seed(self) -> int | str:
        return self.score.get("seed", "?")

    @property
    def tiers(self) -> str:
        return "+".join(self.recon.get("tiers_run", []))

    @property
    def records(self) -> int:
        return int(self.score.get("n_records", 0))

    @property
    def reproduce(self) -> str:
        return self.score.get("reproduce", "")

    def link(self, kind: str) -> dict:
        return self.score.get("links", {}).get(kind, {})

    @property
    def totals(self) -> dict:
        return self.score.get("totals", {})

    # -- findings ----------------------------------------------------------
    @property
    def findings(self) -> list[dict]:
        return self.recon.get("findings", [])

    @property
    def links(self) -> list[dict]:
        return self.recon.get("links", [])

    @property
    def explanations(self) -> dict[str, str]:
        return self.recon.get("explanations", {})

    def by_action(self) -> dict[str, list[dict]]:
        """Grouped the way a reviewer works: decisions first, noise last."""
        out: dict[str, list[dict]] = {a: [] for a in ACTION_ORDER}
        for f in self.findings:
            out.setdefault(f.get("action", "escalated"), []).append(f)
        return {a: rows for a, rows in out.items() if rows}

    def codes_present(self) -> list[str]:
        return sorted({f["code"] for f in self.findings if f.get("code")})

    def filtered(self, actions: list[str] | None = None,
                 codes: list[str] | None = None,
                 tiers: list[str] | None = None,
                 query: str = "") -> list[dict]:
        rows = self.findings
        if actions:
            rows = [f for f in rows if f.get("action") in actions]
        if codes:
            rows = [f for f in rows if (f.get("code") or "--") in codes]
        if tiers:
            rows = [f for f in rows if f.get("tier") in tiers]
        if query:
            q = query.strip().lower()
            rows = [f for f in rows
                    if q in f.get("subject_id", "").lower()
                    or q in (f.get("detail") or "").lower()
                    or q in (f.get("rule") or "").lower()]
        return sorted(rows, key=lambda f: -abs(f.get("amount_delta") or 0))

    # -- tier attribution --------------------------------------------------
    def work_by_tier(self) -> dict[str, dict[str, int]]:
        """Which tier actually did the work, read off the artefact.

        Not a re-run of the pipeline at different ceilings -- every link and
        finding already records the tier that produced it.
        """
        out: dict[str, dict[str, int]] = {}
        for l in self.links:
            row = out.setdefault(l.get("tier", "?"), {"links": 0, "findings": 0})
            row["links"] += 1
        for f in self.findings:
            row = out.setdefault(f.get("tier", "?"), {"links": 0, "findings": 0})
            row["findings"] += 1
        return dict(sorted(out.items()))

    # -- audit trail -------------------------------------------------------
    def audit(self, subject_id: str) -> dict:
        """Everything the pipeline decided about one record.

        This is the "would you trust it" question made concrete: one id in,
        every link and finding that touched it out, each with the rule that
        produced it and the evidence behind it.
        """
        sid = subject_id.strip()
        if not sid:
            return {"subject_id": "", "links": [], "findings": [], "explanation": None}
        links = [l for l in self.links
                 if sid in (l.get("order_id"), l.get("entry_id"),
                            l.get("settlement_id"), l.get("line_id"))]
        findings = [f for f in self.findings if f.get("subject_id") == sid]
        note = next(
            (self.explanations.get(f"{f['subject_type']}:{sid}")
             for f in findings
             if self.explanations.get(f"{f['subject_type']}:{sid}")),
            None,
        )
        return {"subject_id": sid, "links": links, "findings": findings,
                "explanation": note}

    def subject_ids(self) -> list[str]:
        ids = {f["subject_id"] for f in self.findings}
        for l in self.links:
            for k in ("order_id", "entry_id", "settlement_id", "line_id"):
                if l.get(k):
                    ids.add(l[k])
        return sorted(ids)


def load_board(out_dir: Path | None = None) -> Board:
    out_dir = Path(out_dir) if out_dir else _out_dir()
    recon_p, score_p = out_dir / "recon.json", out_dir / "score.json"
    missing = [p.name for p in (recon_p, score_p) if not p.exists()]
    if missing:
        raise MissingArtifacts(
            f"{', '.join(missing)} not found in {out_dir}. Run:\n"
            "  python -m ledgerlock generate --profile default --seed 42\n"
            "  python -m ledgerlock run --upto t3\n"
            "  python -m ledgerlock eval"
        )
    return Board(
        recon=json.loads(recon_p.read_text(encoding="utf-8")),
        score=json.loads(score_p.read_text(encoding="utf-8")),
    )


def load_sweep(out_dir: Path | None = None) -> str | None:
    p = (Path(out_dir) if out_dir else _out_dir()) / "sweep.md"
    return p.read_text(encoding="utf-8") if p.exists() else None


def fmt_paise(amount: int | None) -> str:
    """Indian grouping, matching the CLI exactly."""
    from .domain.money import fmt
    return fmt(int(amount or 0))
