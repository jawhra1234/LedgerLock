"""The exception queue: what a human has to look at, and why.

Grouped by *what was done about it* rather than by exception code, because that
is the order a reviewer works in -- the twenty things needing a decision first,
the two hundred deferred ones last. Every row carries the rule that produced it
and the evidence behind it, so nothing here has to be taken on trust.

Plain-English notes come from T3 when a model was available. They are labelled
as model-written, never mixed in with the machine evidence, and the queue is
fully usable without them.
"""

from __future__ import annotations

from pathlib import Path

from rich.panel import Panel
from rich.table import Table

from .domain.money import fmt
from .domain.taxonomy import EXCEPTION_META
from .pipeline.result import Action, Finding, ReconResult

ORDER = (Action.ESCALATED, Action.DEFERRED, Action.OUT_OF_SCOPE,
         Action.AUTO_RESOLVED, Action.EXPLAINED)

HEAD: dict[Action, tuple[str, str]] = {
    Action.ESCALATED: ("needs a human", "red"),
    Action.DEFERRED: ("nothing wrong, revisit next cycle", "cyan"),
    Action.OUT_OF_SCOPE: ("real, but not gateway money", "blue"),
    Action.AUTO_RESOLVED: ("closed automatically, reported anyway", "green"),
    Action.EXPLAINED: ("accounted for by other findings", "white"),
}


def _label(f: Finding) -> str:
    return EXCEPTION_META[f.code].label if f.code else "unnamed break"


def _total(rows: list[Finding]) -> int:
    return sum(abs(f.amount_delta or 0) for f in rows)


def render_queue(result: ReconResult, console, out: Path, top: int) -> Path:
    md: list[str] = ["# Exception queue", ""]
    md += [f"{len(result.findings)} findings across "
           f"{result.n_orders + result.n_entries + result.n_bank_lines:,} records, "
           f"tiers {'+'.join(t.value for t in result.tiers_run)}.", ""]

    for action in ORDER:
        rows = [f for f in result.findings if f.action is action]
        if not rows:
            continue
        label, colour = HEAD[action]
        console.print(f"\n[{colour}][bold]{action.value.upper()}[/bold] -- {label} "
                      f"({len(rows)} items, {fmt(_total(rows))})[/{colour}]")
        md += [f"## {action.value} -- {label}", "",
               f"{len(rows)} items, {fmt(_total(rows))} involved", "",
               "| code | exception | n | amount |", "|---|---|---|---|"]

        by_code: dict = {}
        for f in rows:
            by_code.setdefault(f.code, []).append(f)

        t = Table(box=None, pad_edge=False)
        for col, just in (("code", "left"), ("exception", "left"),
                          ("n", "right"), ("amount", "right")):
            t.add_column(col, justify=just)
        for code in sorted(by_code, key=lambda c: c.value if c else "zz"):
            items = by_code[code]
            name = EXCEPTION_META[code].label if code else "unnamed break"
            amt = fmt(_total(items))
            shown = code.value if code else "--"
            t.add_row(shown, name, str(len(items)), amt)
            md.append(f"| {shown} | {name} | {len(items)} | {amt} |")
        console.print(t)
        md.append("")

        for code in sorted(by_code, key=lambda c: c.value if c else "zz"):
            note = result.explanations.get(f"code:{code.value}") if code else None
            if not note:
                continue
            console.print(Panel(note, title=f"{code.value} (model-written)",
                                border_style=colour, expand=False))
            md += [f"**{code.value}** (model-written) -- {note}", ""]

    detail = sorted((f for f in result.findings if f.action is Action.ESCALATED),
                    key=lambda f: -abs(f.amount_delta or 0))[:top]
    if detail:
        console.print(f"\n[bold]largest {len(detail)} open items[/bold]")
        md += [f"## Largest {len(detail)} open items", ""]
        for f in detail:
            console.print(f"  [bold]{f.subject_key()}[/bold] "
                          f"{fmt(abs(f.amount_delta or 0))} -- {_label(f)} "
                          f"[dim]({f.rule}, {f.tier.value}, "
                          f"confidence {f.confidence:.2f})[/dim]")
            console.print(f"      [dim]{f.detail}[/dim]")
            md += [f"### {f.subject_key()} -- {fmt(abs(f.amount_delta or 0))}", "",
                   f"- **{_label(f)}** via `{f.rule}` at `{f.tier.value}`, "
                   f"confidence {f.confidence:.2f}",
                   f"- machine evidence: {f.detail}"]
            note = result.explanations.get(f.subject_key())
            if note:
                console.print(f"      [italic]{note}[/italic]")
                md.append(f"- model-written: {note}")
            md.append("")

    if result.llm:
        n = result.llm
        console.print(f"\n[dim]model: {n.get('provider')} / {n.get('mode')} -- "
                      f"{n.get('calls_made', 0)} calls, "
                      f"{n.get('cache_hits', 0)} from cache[/dim]")
        md += ["## Model use", "",
               f"- provider `{n.get('provider')}`, mode `{n.get('mode')}`",
               f"- {n.get('calls_made', 0)} live calls, "
               f"{n.get('cache_hits', 0)} served from committed cache",
               f"- models consulted: {n.get('models_used')}", ""]

    out.mkdir(parents=True, exist_ok=True)
    path = out / "queue.md"
    path.write_text("\n".join(md), encoding="utf-8")
    return path
