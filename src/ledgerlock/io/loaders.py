"""Readers for the three sources.

`load_sources` refuses to read anything under a truth/ directory. That guard is
not decoration: it is the mechanical reason the pipeline cannot cheat, and it
is why the accuracy numbers this project reports are worth reading.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

from ..domain.models import BankLine, Order, PGEntry, TruthException, TruthLink

T = TypeVar("T", bound=BaseModel)


class TruthLeak(RuntimeError):
    """Raised when pipeline code tries to read ground truth."""


def _guard(path: Path) -> None:
    if "truth" in {p.lower() for p in path.parts}:
        raise TruthLeak(
            f"{path} is ground truth; the pipeline must not read it. "
            "Load it only from eval code via load_truth()."
        )


def _read(path: Path, model: type[T]) -> list[T]:
    if not path.exists() or not path.read_text(encoding="utf-8").strip():
        return []
    with path.open(newline="", encoding="utf-8") as fh:
        # Blank cells mean "absent", so drop them and let the model default
        # apply. This keeps None and 0 distinguishable on optional columns.
        return [model(**{k: v for k, v in row.items() if v != ""})
                for row in csv.DictReader(fh)]


@dataclass(frozen=True)
class Sources:
    orders: list[Order]
    entries: list[PGEntry]
    bank_lines: list[BankLine]

    def summary(self) -> str:
        return (f"{len(self.orders)} orders, {len(self.entries)} gateway entries, "
                f"{len(self.bank_lines)} bank lines")


def load_sources(raw_dir: Path) -> Sources:
    _guard(raw_dir)
    return Sources(
        orders=_read(raw_dir / "orders.csv", Order),
        entries=_read(raw_dir / "pg_entries.csv", PGEntry),
        bank_lines=_read(raw_dir / "bank_statement.csv", BankLine),
    )


@dataclass(frozen=True)
class Truth:
    links: list[TruthLink]
    exceptions: list[TruthException]


def load_truth(truth_dir: Path) -> Truth:
    """For eval only. Deliberately not importable from ledgerlock.pipeline."""
    return Truth(
        links=_read(truth_dir / "truth_links.csv", TruthLink),
        exceptions=_read(truth_dir / "truth_exceptions.csv", TruthException),
    )
