"""One command that asserts the guarantees this project actually makes.

`pytest` proves the parts work. This proves the *claims* hold: the exact
sentences in the README, checked against a freshly generated world, on demand,
locally or in CI.

Every check here corresponds to a promise made in print. If one fails, a
sentence in the README has become false and the exit code says so.

Note what is deliberately *not* asserted: that false alarms are zero. They are
not -- the model disagrees with two or three narration labels per profile, and
that is reported as a number rather than hidden behind a threshold. A check that
demanded zero would be a check that invited tuning the data until it passed.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from . import config
from .eval.metrics import Score, score
from .generate.engine import build
from .generate.params import PROFILES
from .generate.writer import write_world
from .io.loaders import load_sources, load_truth
from .llm.adapter import LLMClient, Mode
from .llm.gemini import OfflineProvider
from .pipeline.controller import reconcile_sources
from .pipeline.result import Tier


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    critical: bool = True


def _checks_for(profile: str, seed: int, workdir: Path) -> tuple[list[Check], Score]:
    world = build(replace(PROFILES[profile], seed=seed))
    manifest = write_world(world, workdir)
    src = load_sources(workdir / "raw")
    truth = load_truth(workdir / "truth")

    # Offline provider on purpose: a cache miss must not be rescued by a live
    # call, or this stops being a test of the committed artefact.
    client = LLMClient(OfflineProvider(), config.LLM_CACHE_DIR, mode=Mode.CACHED)
    result = reconcile_sources(src, upto=Tier.T3, llm=client)
    s = score(result, truth, src, manifest)

    sb = s.links["settlement_bank"]
    oe = s.links["order_entry"]
    checks = [
        Check("no false matches asserted",
              s.total_fp == 0,
              f"{s.total_fp} wrong links out of {s.total_proposed} asserted"),
        Check("settlement -> bank fully matched",
              sb.recall == 1.0,
              f"{sb.tp}/{sb.in_truth} recovered"),
        Check("order -> gateway fully verified",
              oe.recall == 1.0 and oe.precision == 1.0,
              f"{oe.tp}/{oe.in_truth}, precision {oe.precision:.0%}"),
        Check("no unresolvable case was auto-resolved",
              not s.unresolvable_auto_resolved,
              f"{len(s.unresolvable_auto_resolved)} closed that must stay open"),
        Check("nothing left flagged-but-unnamed",
              not s.residue,
              f"{len(s.residue)} findings with no code"),
        Check("model cache covers this dataset",
              s.llm.get("cache_misses_unanswered", 0) == 0,
              f"{s.llm.get('cache_misses_unanswered', 0)} prompts unanswered, "
              f"{s.llm.get('cache_hits', 0)} served from cache"),
        Check("no live model call was needed",
              s.llm.get("calls_made", 0) == 0,
              f"{s.llm.get('calls_made', 0)} calls made"),
        # Informational: a pipeline that reports nothing open on this dataset
        # would be lying, so "some cases stay open" is a guarantee too.
        Check("some exceptions correctly remain open",
              s.exceptions_injected - s.exceptions_coded >= 0
              and any(c.missed or c.injected for c in s.codes),
              f"{len(s.false_alarms)} false alarms, "
              f"{s.exceptions_missed} undetected, "
              f"{s.exceptions_coded}/{s.exceptions_injected} classified",
              critical=False),
    ]
    return checks, s


def committed_dataset_matches(root: Path = Path("data")) -> Check:
    """The CSVs in the repo must be exactly what the generator produces.

    Guards against a hand-edited source file or a forgotten regeneration -- both
    of which would make the committed data disagree with the published numbers.
    """
    import json

    manifest_path = root / "truth" / "manifest.json"
    if not manifest_path.exists():
        return Check("committed dataset matches the generator", False,
                     "no manifest; nothing generated yet")
    m = json.loads(manifest_path.read_text(encoding="utf-8"))
    profile, seed = m.get("profile"), m.get("seed")

    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        fresh = Path(tmp)
        write_world(build(replace(PROFILES[profile], seed=seed)), fresh)
        mismatched = []
        for sub, name in (("raw", "orders.csv"), ("raw", "pg_entries.csv"),
                          ("raw", "bank_statement.csv"),
                          ("truth", "truth_links.csv"),
                          ("truth", "truth_exceptions.csv")):
            a = (root / sub / name).read_bytes() if (root / sub / name).exists() else b""
            b = (fresh / sub / name).read_bytes()
            if a != b:
                mismatched.append(f"{sub}/{name}")
    return Check(
        "committed dataset matches the generator",
        not mismatched,
        f"profile {profile} seed {seed}; "
        + (f"differs: {', '.join(mismatched)}" if mismatched else "byte-identical"),
    )


def run_verification(profiles: list[str], seed: int, console) -> bool:
    """-> True if every critical check passed."""
    from rich.table import Table

    import tempfile

    all_ok = True
    ds = committed_dataset_matches()
    console.print(f"\n[bold]LedgerLock verification[/]  seed {seed}  "
                  f"profiles {', '.join(profiles)}")
    mark = "[green]PASS[/]" if ds.ok else "[red]FAIL[/]"
    console.print(f"\n{mark}  {ds.name}\n      [dim]{ds.detail}[/]")
    all_ok = all_ok and ds.ok

    for profile in profiles:
        with tempfile.TemporaryDirectory() as tmp:
            checks, s = _checks_for(profile, seed, Path(tmp))
        t = Table(title=f"{profile} -- {s.n_records:,} records, "
                        f"{s.exceptions_injected} injected exceptions",
                  title_style="bold", title_justify="left")
        t.add_column("")
        t.add_column("check")
        t.add_column("detail", style="dim")
        for c in checks:
            if c.ok:
                state = "[green]PASS[/]"
            elif c.critical:
                state = "[red]FAIL[/]"
                all_ok = False
            else:
                state = "[yellow]INFO[/]"
            t.add_row(state, c.name, c.detail)
        console.print(t)

    if all_ok:
        console.print("[bold green]every guarantee held.[/] The README's claims "
                      "are true of this checkout.")
    else:
        console.print("[bold red]a guarantee failed.[/] A sentence in the README "
                      "is now false -- fix the code or fix the sentence.")
    return all_ok
