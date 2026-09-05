"""LedgerLock dashboard.

    streamlit run app.py

A **read-only** view over two artefacts the CLI produced: `data/out/recon.json`
and `data/out/score.json`. It contains no reconciliation logic and never reads
ground truth. Every number here is the number the harness measured -- if this
app recomputed anything it would be a second implementation free to disagree,
and the screen would stop matching the report.

Run the pipeline first:

    python -m ledgerlock generate --profile default --seed 42
    python -m ledgerlock run --upto t3
    python -m ledgerlock eval
"""

from __future__ import annotations

import streamlit as st

from ledgerlock.dashboard import (
    ACTION_LABEL, ACTION_ORDER, MissingArtifacts, fmt_paise, load_board,
    load_sweep,
)

st.set_page_config(page_title="LedgerLock", page_icon=":ledger:", layout="wide")

ACTION_COLOUR = {
    "escalated": "#d9534f",
    "deferred": "#5bc0de",
    "out_of_scope": "#6c8ebf",
    "auto_resolved": "#5cb85c",
    "explained": "#9a9a9a",
}


@st.cache_data
def _board(mtimes: tuple):
    """Cached on the artefacts' modification times.

    The parameter must NOT be underscore-prefixed: Streamlit excludes such
    arguments from the cache key, which would pin the first load forever and
    quietly show a stale dashboard after every new pipeline run. Caught by
    test_the_app_explains_itself_when_nothing_has_been_run, which saw the
    previous test's data survive into a directory that had none.
    """
    return load_board()


def _artifact_mtimes() -> tuple:
    from ledgerlock.dashboard import _out_dir
    out = _out_dir()
    return tuple(p.stat().st_mtime if p.exists() else 0
                 for p in (out / "recon.json", out / "score.json"))


try:
    board = _board(_artifact_mtimes())
except MissingArtifacts as e:
    st.title("LedgerLock")
    st.error("No pipeline output found.")
    st.code(str(e), language="text")
    st.stop()

# ---------------------------------------------------------------------------
# header
# ---------------------------------------------------------------------------

st.title("LedgerLock")
st.caption(
    f"Settlement reconciliation — profile **{board.profile}**, seed "
    f"**{board.seed}**, tiers **{board.tiers}**, {board.records:,} records. "
    "Read-only view of `recon.json` + `score.json`."
)

sb = board.link("settlement_bank")
oe = board.link("order_entry")
t = board.totals

c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("settlement → bank",
          f"{sb.get('recall', 0) * 100:.1f}%",
          f"{sb.get('tp', 0)}/{sb.get('in_truth', 0)} links")
c2.metric("order → gateway",
          f"{oe.get('recall', 0) * 100:.1f}%",
          f"{oe.get('tp', 0)}/{oe.get('in_truth', 0)} verified")
c3.metric("false matches", f"{t.get('false_matches', 0)}",
          "a wrong match costs more than a gap", delta_color="off")
c4.metric("exceptions classified",
          f"{t.get('exceptions_coded', 0)}/{t.get('exceptions_injected', 0)}",
          f"{t.get('exceptions_missed', 0)} undetected", delta_color="off")
c5.metric("records touching a model",
          f"{t.get('records_touching_a_model', 0) * 100:.1f}%",
          f"{board.score.get('llm', {}).get('cache_hits', 0)} cached answers",
          delta_color="off")

if t.get("false_matches", 0) == 0 and t.get("unresolvable_auto_resolved", 0) == 0:
    st.success(
        "No false match asserted, and no case that had to stay open was closed. "
        f"{t.get('false_alarms', 0)} false alarms and "
        f"{t.get('exceptions_missed', 0)} undetected exceptions are reported "
        "rather than hidden — a pipeline reporting nothing open on this dataset "
        "would be lying."
    )
else:
    st.error("A guarantee failed — see the eval report.")

# ---------------------------------------------------------------------------
tab_queue, tab_audit, tab_codes, tab_tiers, tab_sweep = st.tabs(
    ["Exception queue", "Audit one record", "By exception code",
     "Which tier did the work", "Robustness sweep"])

# ---------------------------------------------------------------------------
with tab_queue:
    st.subheader("What a human has to look at, and why")
    grouped = board.by_action()

    cols = st.columns(len(grouped) or 1)
    for col, (action, rows) in zip(cols, grouped.items()):
        total = sum(abs(f.get("amount_delta") or 0) for f in rows)
        col.markdown(
            f"<div style='border-left:4px solid {ACTION_COLOUR.get(action, '#888')};"
            f"padding-left:10px'><b>{action.replace('_', ' ')}</b><br>"
            f"<span style='font-size:1.6em'>{len(rows)}</span><br>"
            f"<small>{ACTION_LABEL.get(action, '')}<br>{fmt_paise(total)}</small>"
            "</div>",
            unsafe_allow_html=True)

    st.divider()
    f1, f2, f3 = st.columns([2, 2, 3])
    available = [a for a in ACTION_ORDER if a in grouped]
    # A default that is not in the options raises StreamlitAPIException, which
    # a run with no findings at all would hit. Rare, but a crash rather than an
    # empty table, so it is guarded rather than assumed away.
    actions = f1.multiselect("action", available,
                             default=[a for a in ("escalated",) if a in available])
    codes = f2.multiselect("exception code", board.codes_present())
    query = f3.text_input("search id, rule or evidence", "")

    rows = board.filtered(actions=actions, codes=codes, query=query)
    st.caption(f"{len(rows)} findings, largest first")

    for f in rows[:200]:
        code = f.get("code") or "unnamed"
        with st.expander(
            f"{code} · {f['subject_type']}:{f['subject_id']} · "
            f"{fmt_paise(f.get('amount_delta'))} · {f.get('action')}"
        ):
            st.write(f"**Evidence** — {f.get('detail', '')}")
            st.caption(
                f"rule `{f.get('rule')}` · tier `{f.get('tier')}` · "
                f"confidence {f.get('confidence', 0):.2f}")
            note = board.explanations.get(f"{f['subject_type']}:{f['subject_id']}") \
                or (board.explanations.get(f"code:{code}") if code != "unnamed" else None)
            if note:
                st.info(f"**Model-written:** {note}")
    if len(rows) > 200:
        st.caption(f"showing the largest 200 of {len(rows)}")

# ---------------------------------------------------------------------------
with tab_audit:
    st.subheader("Everything the pipeline decided about one record")
    st.caption("The 'would you trust it' question, made concrete: one id in, "
               "every link and finding that touched it out, each with its rule "
               "and evidence.")
    ids = board.subject_ids()
    if not ids:
        st.caption("nothing to audit — no links or findings in this run")
        pick = None
    else:
        pick = st.selectbox("record id", ids, index=0,
                            placeholder="pick or type an id")
    if pick:
        a = board.audit(pick)
        if a["explanation"]:
            st.info(f"**Model-written:** {a['explanation']}")
        left, right = st.columns(2)
        with left:
            st.markdown(f"**Links asserted** ({len(a['links'])})")
            if not a["links"]:
                st.caption("none")
            for l in a["links"]:
                st.markdown(
                    f"- `{l['link_type']}` → "
                    f"{l.get('settlement_id') or l.get('order_id') or ''} "
                    f"{l.get('line_id') or l.get('entry_id') or ''}")
                st.caption(f"  rule `{l['rule']}` · tier `{l['tier']}` · "
                           f"confidence {l['confidence']:.2f} — {l['evidence']}")
        with right:
            st.markdown(f"**Findings raised** ({len(a['findings'])})")
            if not a["findings"]:
                st.caption("none")
            for f in a["findings"]:
                st.markdown(f"- **{f.get('code') or 'unnamed'}** — {f['action']} "
                            f"({fmt_paise(f.get('amount_delta'))})")
                st.caption(f"  rule `{f['rule']}` · tier `{f['tier']}` — {f['detail']}")

# ---------------------------------------------------------------------------
with tab_codes:
    st.subheader("Every exception code, injected against found")
    st.caption("`resolvable = none` means the pipeline is *supposed* to leave it "
               "open. Correctly refusing counts as a success.")
    st.dataframe(
        [
            {
                "code": c["code"], "exception": c["label"],
                "resolvable": c["resolvability"],
                "injected": c["injected"], "detected": c["detected"],
                "classified": c["coded"], "unnamed": c["unclassified"],
                "missed": c["missed"],
                "expected tier": c["expected_tier"].split("_")[0],
                "resolved at": ", ".join(c["resolved_at"]) or "—",
            }
            for c in board.score.get("codes", [])
        ],
        use_container_width=True, hide_index=True,
    )
    for lt, why in (board.score.get("excluded_link_types") or {}).items():
        st.caption(f"`{lt}` is excluded from scoring: {why}.")

# ---------------------------------------------------------------------------
with tab_tiers:
    st.subheader("Which tier actually did the work")
    st.caption("Read off the artefact — every link and finding records the tier "
               "that produced it. The model tier emits no links at all.")
    work = board.work_by_tier()
    st.dataframe(
        [{"tier": k, "links asserted": v["links"], "findings raised": v["findings"]}
         for k, v in work.items()],
        use_container_width=True, hide_index=True,
    )
    llm = board.score.get("llm") or {}
    if llm:
        st.markdown(
            f"**Model use** — provider `{llm.get('provider')}`, mode "
            f"`{llm.get('mode')}`, {llm.get('calls_made', 0)} live calls, "
            f"{llm.get('cache_hits', 0)} served from the committed cache.")
        st.caption("Tier 3 proposes no links at any confidence, so it cannot "
                   "create a false match. That guarantee is structural, not "
                   "a property of the model behaving well.")

# ---------------------------------------------------------------------------
with tab_sweep:
    st.subheader("Is seed 42 lucky?")
    sweep = load_sweep()
    if sweep:
        st.markdown(sweep)
    else:
        st.info("No sweep yet. Run `python -m ledgerlock sweep` to generate "
                "`data/out/sweep.md`.")

st.divider()
st.caption(f"Reproduce this dataset: `{board.reproduce}`")
