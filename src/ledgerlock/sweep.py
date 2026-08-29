"""Robustness sweep: the same pipeline over many independently generated worlds.

Every number published elsewhere in this project comes from seed 42. The brief
this was built against says, in as many words, *"one cherry-picked match proves
nothing"* -- and a reader is entitled to ask whether seed 42 is lucky. This
answers that before it is asked, by reporting a distribution instead of a point.

Two deliberate choices:

**It runs T1+T2, not T3.** Tier 3 emits no links at any confidence, so
link-level metrics are identical at T2 and T3 -- sweeping the deterministic
tiers measures exactly the thing that could go wrong. It is also the honest
option: the committed model cache covers seed 42 only, so a T3 sweep would be
reporting on prompts nobody ever answered.

**It builds sources in memory.** The CSV round-trip is already proven field by
field by `test_csv_round_trip_preserves_every_field`, so re-proving it sixty
times would only buy slower sweeps and fewer seeds.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field, replace
from pathlib import Path

from .eval.metrics import score
from .generate.engine import build
from .generate.params import PROFILES
from .io.loaders import Sources, Truth
from .llm.adapter import LLMClient, Mode
from .llm.gemini import OfflineProvider
from .pipeline.controller import reconcile_sources
from .pipeline.result import Tier


@dataclass
class SweepRow:
    profile: str
    seed: int
    records: int
    injected: int
    sb_tp: int
    sb_truth: int
    false_matches: int
    false_alarms: int
    wrongly_resolved: int
    classified: int
    residue: int

    @property
    def recall(self) -> float:
        return self.sb_tp / self.sb_truth if self.sb_truth else 1.0

    @property
    def clean(self) -> bool:
        """The two things that must never happen, on any world."""
        return self.false_matches == 0 and self.wrongly_resolved == 0


@dataclass
class SweepResult:
    rows: list[SweepRow] = field(default_factory=list)
    upto: str = Tier.T2.value

    # -- aggregates --------------------------------------------------------
    @property
    def datasets(self) -> int:
        return len(self.rows)

    @property
    def records(self) -> int:
        return sum(r.records for r in self.rows)

    @property
    def injected(self) -> int:
        return sum(r.injected for r in self.rows)

    @property
    def false_matches(self) -> int:
        return sum(r.false_matches for r in self.rows)

    @property
    def wrongly_resolved(self) -> int:
        return sum(r.wrongly_resolved for r in self.rows)

    @property
    def dirty(self) -> list[SweepRow]:
        return [r for r in self.rows if not r.clean]

    def for_profile(self, profile: str) -> list[SweepRow]:
        return [r for r in self.rows if r.profile == profile]

    def recalls(self, profile: str) -> list[float]:
        return sorted(r.recall for r in self.for_profile(profile))

    def worst(self, profile: str) -> SweepRow | None:
        rows = self.for_profile(profile)
        return min(rows, key=lambda r: (r.recall, r.seed)) if rows else None


def _one(profile: str, seed: int, upto: Tier) -> SweepRow:
    world = build(replace(PROFILES[profile], seed=seed))
    src = Sources(orders=world.orders, entries=world.entries,
                  bank_lines=world.bank_lines)
    truth = Truth(links=world.links, exceptions=world.exceptions)

    # Mode.OFF never reads the cache directory, so the path is inert. The
    # offline provider makes doubly sure nothing can reach the network
    # across sixty worlds.
    client = LLMClient(OfflineProvider(), Path("."), mode=Mode.OFF)
    result = reconcile_sources(src, upto=upto, llm=client)
    s = score(result, truth, src)
    sb = s.links["settlement_bank"]

    return SweepRow(
        profile=profile, seed=seed,
        records=s.n_records, injected=s.exceptions_injected,
        sb_tp=sb.tp, sb_truth=sb.in_truth,
        false_matches=s.total_fp,
        false_alarms=len(s.false_alarms),
        wrongly_resolved=len(s.unresolvable_auto_resolved),
        classified=s.exceptions_coded,
        residue=len(s.residue),
    )


def run_sweep(profiles: list[str], seeds: list[int],
              upto: Tier = Tier.T2, progress=None) -> SweepResult:
    out = SweepResult(upto=upto.value)
    for profile in profiles:
        for seed in seeds:
            out.rows.append(_one(profile, seed, upto))
            if progress is not None:
                progress(profile, seed)
    return out


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------

def _pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def render(res: SweepResult, console) -> None:
    from rich.table import Table

    console.print(
        f"\n[bold]robustness sweep[/]  {res.datasets} independently generated "
        f"worlds, {res.records:,} records, {res.injected:,} injected exceptions, "
        f"tiers up to {res.upto}"
    )

    t = Table(title="settlement -> bank matching, per profile",
              title_style="bold", title_justify="left")
    for c in ("profile", "worlds", "min", "median", "max",
              "false matches", "worst seed"):
        t.add_column(c, justify="left" if c == "profile" else "right")
    for profile in sorted({r.profile for r in res.rows}):
        rows = res.for_profile(profile)
        rec = res.recalls(profile)
        fm = sum(r.false_matches for r in rows)
        worst = res.worst(profile)
        t.add_row(
            profile, str(len(rows)),
            _pct(rec[0]), _pct(statistics.median(rec)), _pct(rec[-1]),
            f"[{'green' if fm == 0 else 'red'}]{fm}[/]",
            f"{worst.seed} ({_pct(worst.recall)})" if worst else "-",
        )
    console.print(t)

    g = Table(show_header=False, box=None, pad_edge=False)
    g.add_column(style="dim")
    g.add_column(justify="right")
    g.add_row("false matches, all worlds",
              f"[{'green' if res.false_matches == 0 else 'red'}]"
              f"{res.false_matches}[/]")
    g.add_row("unresolvable cases wrongly auto-resolved",
              f"[{'green' if res.wrongly_resolved == 0 else 'red'}]"
              f"{res.wrongly_resolved}[/]")
    g.add_row("false alarms, all worlds",
              f"{sum(r.false_alarms for r in res.rows)} [dim](expected: T3 is off, "
              "so these are deterministic-tier only)[/]")
    console.print(g)

    if res.dirty:
        d = Table(title="worlds that broke a guarantee", title_style="bold red")
        for c in ("profile", "seed", "false matches", "wrongly resolved"):
            d.add_column(c)
        for r in res.dirty:
            d.add_row(r.profile, str(r.seed), str(r.false_matches),
                      str(r.wrongly_resolved))
        console.print(d)
    else:
        console.print("[bold green]no world produced a false match "
                      "or closed a case that had to stay open.[/]")


def to_markdown(res: SweepResult) -> str:
    L: list[str] = []
    a = L.append
    a("# Robustness sweep")
    a("")
    a(f"The same pipeline over **{res.datasets} independently generated worlds** "
      f"— {res.records:,} records, {res.injected:,} injected exceptions, "
      f"tiers up to `{res.upto}`.")
    a("")
    a("Every other number in this project comes from seed 42. This reports a "
      "distribution instead of a point, so \"zero false matches\" is a claim "
      "about the matcher rather than about one lucky dataset.")
    a("")
    a("T3 is excluded deliberately: it emits no links, so link metrics are "
      "identical at T2 and T3, and the committed model cache covers seed 42 "
      "only — sweeping it would report on prompts nobody answered.")
    a("")
    a("| profile | worlds | min | median | max | false matches | worst seed |")
    a("|---|---|---|---|---|---|---|")
    for profile in sorted({r.profile for r in res.rows}):
        rows = res.for_profile(profile)
        rec = res.recalls(profile)
        fm = sum(r.false_matches for r in rows)
        worst = res.worst(profile)
        a(f"| `{profile}` | {len(rows)} | {_pct(rec[0])} | "
          f"{_pct(statistics.median(rec))} | {_pct(rec[-1])} | **{fm}** | "
          f"{worst.seed} ({_pct(worst.recall)}) |")
    a("")
    a(f"- **false matches across all {res.datasets} worlds: {res.false_matches}**")
    a(f"- **unresolvable cases wrongly auto-resolved: {res.wrongly_resolved}**")
    a("")
    a("## Every world")
    a("")
    a("| profile | seed | records | injected | settlement→bank | false matches | classified | residue |")
    a("|---|---|---|---|---|---|---|---|")
    for r in res.rows:
        a(f"| `{r.profile}` | {r.seed} | {r.records:,} | {r.injected} | "
          f"{r.sb_tp}/{r.sb_truth} ({_pct(r.recall)}) | {r.false_matches} | "
          f"{r.classified}/{r.injected} | {r.residue} |")
    a("")
    return "\n".join(L)
