"""Rendering the score, for a terminal and for a file.

The report always leads with match rate and false-match rate side by side. A
match rate on its own is the number a demo shows; the pair is the number a
finance team would act on.
"""

from __future__ import annotations

from ..domain.money import fmt
from ..domain.taxonomy import EXCEPTION_META
from .metrics import EXCLUDED_LINK_TYPES, Score

_COLOUR = {"full": "green", "partial": "yellow", "none": "red"}

# The two link types are not the same difficulty, so they are never blended.
# `settlement_bank` is the reconciliation task: the gateway gives a UTR buried
# in a narration and the bank may mangle it, merge two payouts into one credit
# or split one across two. `order_entry` is verification of a reference the
# report already supplies -- real work, since it catches orphans, but a far
# easier population that would drown the headline if averaged in.
HEADLINE_LABELS = {
    "settlement_bank": "settlement -> bank matching",
    "order_entry": "order -> gateway verification",
}


def _pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def _residue_groups(score: Score) -> list[tuple[str, int, int]]:
    """(rule, count, total absolute amount) for everything left unnamed."""
    agg: dict[str, list[int]] = {}
    for f in score.residue:
        row = agg.setdefault(f.rule, [0, 0])
        row[0] += 1
        row[1] += abs(f.amount_delta or 0)
    return sorted(((r, c, a) for r, (c, a) in agg.items()),
                  key=lambda t: -t[1])


def render_console(score: Score, console) -> None:
    from rich.table import Table

    seed = score.manifest.get("seed", "?")
    profile = score.manifest.get("profile", "?")
    console.print(
        f"\n[bold]reconciliation report[/]  profile [bold]{profile}[/] "
        f"seed [bold]{seed}[/]  tiers {'+'.join(score.tiers)}  "
        f"{score.n_records:,} records"
    )

    head = Table(show_header=False, box=None, pad_edge=False)
    head.add_column(style="dim")
    head.add_column(justify="right")
    # Reported per link type on purpose. A single blended rate would be
    # dominated by order_entry, where the join key is handed over in a column,
    # and would read as ~99% while the actual reconciliation task sat lower.
    for lt, label in HEADLINE_LABELS.items():
        s_ = score.links.get(lt)
        if s_ is None:
            continue
        head.add_row(label, f"[bold]{_pct(s_.recall)}[/]"
                            f"  ({s_.tp}/{s_.in_truth})")
    fmr = score.false_match_rate
    head.add_row("false-match rate",
                 f"[{'green' if fmr == 0 else 'red'}]{_pct(fmr)}[/]"
                 f"  ({score.total_fp}/{score.total_proposed} asserted)")
    head.add_row("exceptions detected",
                 f"{score.exceptions_detected}/{score.exceptions_injected}")
    head.add_row("...correctly classified", f"{score.exceptions_coded}")
    head.add_row("...left unnamed (honest residue)", f"{len(score.residue)}")
    head.add_row("...undetected", f"{score.exceptions_missed}")
    console.print(head)

    t = Table(title="links", title_style="bold", title_justify="left")
    for c in ("type", "truth", "asserted", "correct", "wrong", "missed",
              "precision", "recall"):
        t.add_column(c, justify="right" if c != "type" else "left")
    for s in score.links.values():
        t.add_row(s.link_type, str(s.in_truth), str(s.proposed), str(s.tp),
                  f"[{'green' if s.fp == 0 else 'red'}]{s.fp}[/]", str(s.fn),
                  _pct(s.precision), _pct(s.recall))
    console.print(t)
    for lt, why in EXCLUDED_LINK_TYPES.items():
        console.print(f"[dim]excluded from scoring: {lt} -- {why}[/]")

    x = Table(title="exceptions by code", title_style="bold", title_justify="left")
    for c in ("code", "label", "resolvable", "injected", "detected",
              "classified", "unnamed", "missed"):
        x.add_column(c, justify="right" if c not in ("code", "label", "resolvable") else "left")
    for cs in score.codes:
        m = EXCEPTION_META[cs.code]
        r = m.resolvability.value
        missed = f"[red]{cs.missed}[/]" if cs.missed else "0"
        x.add_row(cs.code.value, m.label, f"[{_COLOUR[r]}]{r}[/]",
                  str(cs.injected), str(cs.detected), str(cs.coded),
                  str(cs.unclassified), missed)
    console.print(x)

    if score.residue:
        r = Table(title="honest exception list -- flagged, not yet explained",
                  title_style="bold", title_justify="left")
        r.add_column("rule")
        r.add_column("n", justify="right")
        r.add_column("gross amount at issue", justify="right")
        for rule, n, amount in _residue_groups(score):
            r.add_row(rule, str(n), fmt(amount))
        console.print(r)

    checks = Table(title="integrity checks", title_style="bold", title_justify="left")
    checks.add_column("check")
    checks.add_column("result", justify="right")
    for label, n in (
        ("false matches asserted", score.total_fp),
        ("false alarms on clean records", len(score.false_alarms)),
        ("unresolvable cases wrongly auto-resolved", len(score.unresolvable_auto_resolved)),
    ):
        checks.add_row(label, f"[{'green' if n == 0 else 'red'}]{n}[/]")
    checks.add_row("corroborating flags on related records",
                   f"[dim]{len(score.corroborating)}[/]")
    console.print(checks)


def to_markdown(score: Score) -> str:
    seed = score.manifest.get("seed", "?")
    profile = score.manifest.get("profile", "?")
    reproduce = score.manifest.get("reproduce", "")
    L: list[str] = []
    a = L.append

    a("# Reconciliation report")
    a("")
    a(f"- profile **{profile}**, seed **{seed}**, tiers **{'+'.join(score.tiers)}**")
    a(f"- {score.n_records:,} source records")
    if reproduce:
        a(f"- regenerate this dataset: `{reproduce}`")
    a("")
    a("## Headline")
    a("")
    a("| metric | value |")
    a("|---|---|")
    for lt, label in HEADLINE_LABELS.items():
        s_ = score.links.get(lt)
        if s_ is not None:
            a(f"| {label} | **{_pct(s_.recall)}** ({s_.tp}/{s_.in_truth}) |")
    a(f"| **false-match rate** | **{_pct(score.false_match_rate)}** "
      f"({score.total_fp}/{score.total_proposed} asserted) |")
    a(f"| exceptions detected | {score.exceptions_detected}/{score.exceptions_injected} |")
    a(f"| ...correctly classified | {score.exceptions_coded} |")
    a(f"| ...left unnamed | {len(score.residue)} |")
    a(f"| ...undetected | {score.exceptions_missed} |")
    a("")
    oe = score.links.get("order_entry")
    n_oe = oe.in_truth if oe else 0
    blended = _pct(score.match_rate)
    a(f"Deliberately **not** blended into one figure. `order_entry` is {n_oe} "
      "links where the gateway hands over the join key in a column;")
    a("`settlement_bank` is the actual reconciliation. Averaging them would")
    a(f"report {blended} and hide the number that matters.")
    a("")
    a("A match rate is only meaningful beside a false-match rate. A gap gets")
    a("investigated; a false match gets posted.")
    a("")
    a("## Links")
    a("")
    a("| type | truth | asserted | correct | wrong | missed | precision | recall |")
    a("|---|---|---|---|---|---|---|---|")
    for s in score.links.values():
        a(f"| `{s.link_type}` | {s.in_truth} | {s.proposed} | {s.tp} | {s.fp} | "
          f"{s.fn} | {_pct(s.precision)} | {_pct(s.recall)} |")
    a("")
    for lt, why in EXCLUDED_LINK_TYPES.items():
        a(f"> `{lt}` is excluded from scoring: {why}.")
    a("")
    a("## Exceptions by code")
    a("")
    a("| code | label | resolvable | expected tier | injected | detected | classified | unnamed | missed |")
    a("|---|---|---|---|---|---|---|---|---|")
    for cs in score.codes:
        m = EXCEPTION_META[cs.code]
        a(f"| {cs.code.value} | {m.label} | {m.resolvability.value} | "
          f"{m.expected_tier.value} | {cs.injected} | {cs.detected} | "
          f"{cs.coded} | {cs.unclassified} | {cs.missed} |")
    a("")
    if score.residue:
        a("## Honest exception list")
        a("")
        a("Flagged, with the evidence recorded, but not yet explained. These are")
        a("the cases a later tier has to earn -- not cases quietly dropped.")
        a("")
        a("| rule | n | gross amount at issue |")
        a("|---|---|---|")
        for rule, n, amount in _residue_groups(score):
            a(f"| `{rule}` | {n} | {fmt(amount)} |")
        a("")
    undetected = [cs for cs in score.codes if cs.missed]
    if undetected:
        a("## Not detected at all")
        a("")
        for cs in undetected:
            m = EXCEPTION_META[cs.code]
            a(f"- **{cs.code.value} {m.label}** -- {cs.missed} of {cs.injected} "
              f"missed; expected at {m.expected_tier.value}")
        a("")
    a("## Integrity checks")
    a("")
    a("| check | result |")
    a("|---|---|")
    a(f"| false matches asserted | {score.total_fp} |")
    a(f"| false alarms on clean records | {len(score.false_alarms)} |")
    a(f"| unresolvable cases wrongly auto-resolved | {len(score.unresolvable_auto_resolved)} |")
    a(f"| corroborating flags on related records | {len(score.corroborating)} |")
    a("")
    return "\n".join(L) + "\n"
