"""Command line entry point.

    python -m ledgerlock generate --profile default --seed 42
    python -m ledgerlock inspect
"""

from __future__ import annotations

import json
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from .domain.money import fmt
from .domain.taxonomy import EXCEPTION_META, ExceptionCode, Resolvability
from .generate.params import PROFILES
from .generate.writer import write_world
from .io.loaders import load_sources

app = typer.Typer(add_completion=False, help="LedgerLock -- AI finance controller.")
console = Console()

DATA = Path("data")


@app.command()
def generate(
    profile: str = typer.Option("default", help=f"one of {', '.join(PROFILES)}"),
    seed: int = typer.Option(42, help="same seed + profile => byte-identical dataset"),
    out: Path = typer.Option(DATA, help="root containing raw/ and truth/"),
) -> None:
    """Build a synthetic settlement world with relational ground truth."""
    from dataclasses import replace

    from .generate.engine import build

    if profile not in PROFILES:
        raise typer.BadParameter(f"unknown profile {profile!r}")
    spec = replace(PROFILES[profile], seed=seed)

    with console.status(f"generating [bold]{profile}[/] (seed {seed})..."):
        world = build(spec)
        manifest = write_world(world, out)

    console.print(f"[green]clean-world identity verified[/] across "
                  f"{manifest['settlements']} settlements before injection")

    t = Table(title=f"{profile} / seed {seed}", title_style="bold")
    t.add_column("file")
    t.add_column("rows", justify="right")
    for name, n in manifest["counts"].items():
        t.add_row(name, f"{n:,}")
    console.print(t)

    x = Table(title="injected exceptions", title_style="bold")
    x.add_column("code")
    x.add_column("label")
    x.add_column("resolvable")
    x.add_column("tier")
    x.add_column("n", justify="right")
    for code, info in manifest["exceptions_by_code"].items():
        colour = {"full": "green", "partial": "yellow", "none": "red"}[info["resolvability"]]
        x.add_row(code, info["label"], f"[{colour}]{info['resolvability']}[/]",
                  info["expected_tier"].replace("_", " "), str(info["injected"]))
    console.print(x)

    unresolvable = sum(i["injected"] for i in manifest["exceptions_by_code"].values()
                       if i["resolvability"] != "full")
    total = sum(i["injected"] for i in manifest["exceptions_by_code"].values())
    console.print(
        f"{total} exceptions, of which [red]{unresolvable}[/] are not fully "
        "auto-resolvable by design -- the ceiling any honest matcher can hit."
    )
    console.print(f"\nraw   -> {out / 'raw'}\ntruth -> {out / 'truth'}")


@app.command()
def inspect(raw: Path = typer.Option(DATA / "raw")) -> None:
    """Read back the generated sources and show the cash position."""
    src = load_sources(raw)
    console.print(f"[bold]{src.summary()}[/]")
    credits = sum(b.credit for b in src.bank_lines)
    debits = sum(b.debit for b in src.bank_lines)
    closing = src.bank_lines[-1].balance if src.bank_lines else 0
    t = Table(show_header=False)
    t.add_row("bank credits", fmt(credits))
    t.add_row("bank debits", fmt(debits))
    t.add_row("closing balance", fmt(closing))
    # Every settled batch nets to exactly zero across its member entries, its
    # tax/reserve rows and its payout row -- so the sum of all entry nets is
    # precisely the gateway balance still to be settled. It runs negative when
    # pending chargebacks outweigh captures caught by the statement cut-off.
    unsettled = [e for e in src.entries if e.settlement_id is None]
    t.add_row("gateway balance unsettled", fmt(sum(e.net for e in src.entries)))
    t.add_row("unsettled entries", f"{len(unsettled):,}")
    console.print(t)


@app.command()
def taxonomy() -> None:
    """Print the exception taxonomy this project is measured against."""
    t = Table(title="exception taxonomy", title_style="bold")
    for c in ("code", "label", "resolvable", "expected tier", "meaning"):
        t.add_column(c)
    for code in ExceptionCode:
        m = EXCEPTION_META[code]
        colour = {Resolvability.FULL: "green", Resolvability.PARTIAL: "yellow",
                  Resolvability.NONE: "red"}[m.resolvability]
        t.add_row(code.value, m.label, f"[{colour}]{m.resolvability.value}[/]",
                  m.expected_tier.value.replace("_", " "), m.description)
    console.print(t)


@app.command()
def run(raw: Path = typer.Option(DATA / "raw")) -> None:
    """Reconcile (tier 1-3). Not built yet."""
    console.print("[yellow]not implemented yet[/] -- reconciliation tiers land next.")
    raise typer.Exit(1)


@app.command("eval")
def eval_cmd() -> None:
    """Score the pipeline against ground truth. Not built yet."""
    console.print("[yellow]not implemented yet[/] -- needs `run` first.")
    raise typer.Exit(1)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
