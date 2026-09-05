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

from . import config
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
def run(
    raw: Path = typer.Option(DATA / "raw"),
    out: Path = typer.Option(DATA / "out"),
    upto: str = typer.Option("t2", help="highest tier to run: t1, t2 or t3"),
    llm: str = typer.Option(
        "cached",
        help="off | cached (committed responses, no key needed) | live (calls the API)"),
    allow_cache_miss: bool = typer.Option(
        False, "--allow-cache-miss",
        help="accept unanswered prompts in cached mode instead of failing"),
) -> None:
    """Reconcile the sources and write the result artifact."""
    from dotenv import load_dotenv

    from .llm.adapter import CacheIncomplete, LLMClient, Mode
    from .llm.gemini import build_provider
    from .pipeline.controller import reconcile_sources
    from .pipeline.result import Tier

    try:
        ceiling = Tier(upto.lower())
    except ValueError:
        raise typer.BadParameter(f"unknown tier {upto!r}; use t1, t2 or t3")
    try:
        mode = Mode(llm.lower())
    except ValueError:
        raise typer.BadParameter(f"unknown llm mode {llm!r}; use off, cached or live")

    load_dotenv()
    provider = build_provider(prefer_live=mode is Mode.LIVE)
    if mode is Mode.LIVE and provider.name == "offline":
        console.print("[yellow]no GEMINI_API_KEY found[/] -- falling back to cache only.")
        mode = Mode.CACHED
    client = LLMClient(provider, config.LLM_CACHE_DIR, mode=mode)

    src = load_sources(raw)
    result = reconcile_sources(src, upto=ceiling, llm=client)

    # Checked before anything is written, so an incomplete run cannot leave a
    # recon.json behind for `eval` to score as if it were whole.
    if not allow_cache_miss:
        try:
            client.assert_complete()
        except CacheIncomplete as e:
            console.print("[bold red]incomplete model cache[/]")
            console.print(str(e))
            raise typer.Exit(1)

    out.mkdir(parents=True, exist_ok=True)
    (out / "recon.json").write_text(
        result.model_dump_json(indent=2), encoding="utf-8")

    console.print(f"[bold]{src.summary()}[/], {result.n_settlements} settlements")
    t = Table(show_header=False, box=None)
    t.add_column(style="dim")
    t.add_column(justify="right")
    t.add_row("tiers run", "+".join(t_.value for t_ in result.tiers_run))
    t.add_row("links asserted", f"{len(result.links):,}")
    t.add_row("findings raised", f"{len(result.findings):,}")
    t.add_row("...escalated to a human", f"{len(result.escalated):,}")
    t.add_row("...unnamed (honest residue)", f"{len(result.unclassified):,}")
    auto = sum(1 for f in result.findings if f.action.value == "auto_resolved")
    t.add_row("...auto-resolved", f"{auto:,}")
    if result.llm:
        n = result.llm
        t.add_row("model calls", f"{n.get('calls_made', 0):,}")
        t.add_row("...served from cache", f"{n.get('cache_hits', 0):,}")
        touched = n.get("calls_made", 0) + n.get("cache_hits", 0)
        records = result.n_orders + result.n_entries + result.n_bank_lines
        t.add_row("records touching a model",
                  f"{touched / records * 100:.1f}%" if records else "-")
    console.print(t)

    by_rule: dict[str, int] = {}
    for f in result.findings:
        by_rule[f.rule] = by_rule.get(f.rule, 0) + 1
    r = Table(title="findings by rule", title_style="bold", title_justify="left")
    r.add_column("rule")
    r.add_column("n", justify="right")
    for rule, n in sorted(by_rule.items(), key=lambda kv: -kv[1]):
        r.add_row(rule, str(n))
    console.print(r)
    console.print()
    console.print(f"-> {out / 'recon.json'}")


@app.command("eval")
def eval_cmd(
    raw: Path = typer.Option(DATA / "raw"),
    truth: Path = typer.Option(DATA / "truth"),
    out: Path = typer.Option(DATA / "out"),
) -> None:
    """Score the last `run` against ground truth."""
    import json

    from .eval.metrics import score
    from .eval.report import render_console, to_markdown
    from .io.loaders import load_truth
    from .pipeline.result import ReconResult

    artifact = out / "recon.json"
    if not artifact.exists():
        console.print("[red]no recon.json[/] -- run `python -m ledgerlock run` first.")
        raise typer.Exit(1)

    result = ReconResult.model_validate_json(artifact.read_text(encoding="utf-8"))
    manifest_path = truth / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))         if manifest_path.exists() else {}

    s = score(result, load_truth(truth), load_sources(raw), manifest)
    render_console(s, console)
    report = out / "report.md"
    report.write_text(to_markdown(s), encoding="utf-8", newline="\n")
    # Machine-readable twin, so the dashboard can show a score without
    # recomputing one. See eval.metrics.score_to_dict.
    from .eval.metrics import score_to_dict
    (out / "score.json").write_text(
        json.dumps(score_to_dict(s), indent=2, default=str),
        encoding="utf-8", newline="\n")
    console.print()
    console.print(f"-> {report}")


@app.command()
def queue(
    out: Path = typer.Option(DATA / "out"),
    top: int = typer.Option(config.QUEUE_TOP_N, help="individual items to detail"),
) -> None:
    """The exception queue: what a human has to look at, and why."""
    from .queueview import render_queue
    from .pipeline.result import ReconResult

    artifact = out / "recon.json"
    if not artifact.exists():
        console.print("[red]no recon.json[/] -- run `python -m ledgerlock run` first.")
        raise typer.Exit(1)
    result = ReconResult.model_validate_json(artifact.read_text(encoding="utf-8"))
    path = render_queue(result, console, out, top)
    console.print()
    console.print(f"-> {path}")


@app.command()
def sweep(
    profiles: str = typer.Option("smoke,default,scale", help="comma-separated"),
    seeds: int = typer.Option(20, help="how many consecutive seeds to run"),
    start_seed: int = typer.Option(1),
    upto: str = typer.Option("t2", help="t1 or t2; t3 emits no links, so t2 is the ceiling that matters"),
    out: Path = typer.Option(DATA / "out"),
) -> None:
    """Run the pipeline over many independent worlds and report the spread."""
    from .generate.params import PROFILES
    from .pipeline.result import Tier
    from .sweep import render, run_sweep, to_markdown

    names = [p.strip() for p in profiles.split(",") if p.strip()]
    for n in names:
        if n not in PROFILES:
            raise typer.BadParameter(f"unknown profile {n!r}")
    try:
        ceiling = Tier(upto.lower())
    except ValueError:
        raise typer.BadParameter(f"unknown tier {upto!r}")
    if ceiling is Tier.T3:
        raise typer.BadParameter(
            "t3 needs a committed cache per seed and emits no links anyway; use t2")

    seed_list = list(range(start_seed, start_seed + seeds))
    total = len(names) * len(seed_list)
    with console.status("") as status:
        def tick(profile: str, seed: int) -> None:
            tick.n += 1
            status.update(f"sweeping [bold]{profile}[/] seed {seed} "
                          f"({tick.n}/{total})")
        tick.n = 0
        result = run_sweep(names, seed_list, ceiling, progress=tick)

    render(result, console)
    out.mkdir(parents=True, exist_ok=True)
    path = out / "sweep.md"
    path.write_text(to_markdown(result), encoding="utf-8", newline="\n")
    console.print()
    console.print(f"-> {path}")
    if result.dirty:
        raise typer.Exit(1)


@app.command("verify")
def verify_cmd(
    profile: str = typer.Option("all", help="smoke | default | scale | all"),
    seed: int = typer.Option(42),
) -> None:
    """Assert every guarantee this project makes, and exit non-zero if one fails."""
    from .generate.params import PROFILES
    from .verify import run_verification

    names = list(PROFILES) if profile == "all" else [profile]
    for n in names:
        if n not in PROFILES:
            raise typer.BadParameter(f"unknown profile {n!r}")
    if not run_verification(names, seed, console):
        raise typer.Exit(1)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
