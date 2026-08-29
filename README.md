# LedgerLock

[![ci](https://github.com/jawhra1234/LedgerLock/actions/workflows/ci.yml/badge.svg)](https://github.com/jawhra1234/LedgerLock/actions/workflows/ci.yml)

**An AI finance controller that closes the settlement reconciliation loop for a
payment-gateway merchant — and reports its own failures.**

A merchant gets one lump credit in their bank account. It is net of MDR, GST on
that MDR, refunds, chargebacks, TDS under s.194-O and a rolling reserve.
Somewhere inside that single number are a few hundred individual orders.

LedgerLock reconciles that three ways — **ERP orders ↔ gateway ledger ↔ bank
statement** — scores itself against an answer key written before the matcher
existed, and hands back an exception list it does not pretend to have solved.

---

## Results

Profile `default`, seed 42: **1,126 source records, 86 injected exceptions.**
Each tier is measured separately on the same dataset (`run --upto t1|t2|t3`), so
the contribution of each is evidence rather than a claim.

| metric | T1 deterministic | T2 rules + search | T3 model-assisted |
|---|---|---|---|
| **settlement → bank matching** | 90.5% (19/21) | **100%** (21/21) | **100%** (21/21) |
| order → gateway verification | 100% (508/508) | 100% | 100% |
| **false matches asserted** | **0** | **0** | **0** |
| exceptions classified | 44/86 | 83/86 | **85/86** |
| flagged but unnamed | 24 | 0 | 0 |
| undetected | 20 | 3 | **1** |
| false alarms on clean records | 0 | 0 | 2 |
| records touching a model | 0% | 0% | **2.7%** |

The same pipeline, unchanged, at three sizes — so the result is a property of the
matcher, not of one convenient dataset:

| profile | records | exceptions | T1 | T2 | T3 | classified | false matches | model-touched |
|---|---|---|---|---|---|---|---|---|
| `smoke` | 191 | 18 | 88.9% | 100% | 100% | 18/18 | **0** | 13.1% |
| `default` | 1,126 | 86 | 90.5% | 100% | 100% | 85/86 | **0** | 2.7% |
| `scale` | 10,450 | 797 | 89.4% | 100% | 100% | 794/797 | **0** | 0.7% |

**Zero false matches at every size and every tier.** 154 tests. Every figure
above reproduces from a clean clone **with no API key**.

### Is seed 42 lucky?

Every number above comes from one seed, so the same pipeline was run over **220
independently generated worlds** across three disjoint seed ranges —
**582,053 records, 44,232 injected exceptions**:

| seed range | profiles | worlds | settlement → bank | false matches |
|---|---|---|---|---|
| 1–20 | all three | 60 | 100% (min = median = max) | **0** |
| 500–519 | all three | 60 | 100% (min = median = max) | **0** |
| 1000–1099 | `default` | 100 | 100% (min = median = max) | **0** |

**Zero false matches and zero wrongly-closed cases across all 220.**
`python -m ledgerlock sweep` reproduces the first range in about 80 seconds.

Three separate ranges because one range could itself be lucky.

#### The flat 100% is a claim too, so it was attacked three ways

**Are the hard cases even present?** If some worlds had no mangled UTR or merged
credit, 100% would be trivial. All 60 checked: **0 worlds with no hard cases**
— 2 per world at `smoke`/`default`, 7 at `scale`.

**Can the sweep tell a worse pipeline from a better one?** The same 60 worlds at
T1 only, where the spread is real:

```
default   min 90.0%   median 90.9%   worst seed 10
scale     min 89.4%   median 89.4%   worst seed 1
smoke     min 81.8%   median 87.9%   worst seed 13
```

**Can it report a false match at all?** R11 was sabotaged — its uniqueness
requirement stripped so it links any unclaimed credit regardless of amount,
which is exactly the confident wrong match this project exists to avoid:

```
baseline (correct R11):    0 false matches,  0 dirty worlds
sabotaged R11:             7 false matches,  6/6 dirty worlds, seeds named
```

That is a permanent test — `test_the_sweep_catches_a_deliberately_broken_matcher`
— and removing the sabotage makes it fail, so it is not a no-op either.

#### What the sweep does and does not prove

It runs T1+T2, not T3: tier 3 emits no links, so link metrics are identical at
T2 and T3, and the committed model cache covers seed 42 only.

Flat 100% is the *expected* shape here, not a surprise: T2 recovers mangled UTRs
by exact amount-and-date and merged credits by exact subset-sum, and exact
arithmetic that works at all works on every world. **The number worth reading is
the 0 false matches**, because that is the property with real engineering behind
it — uniqueness requirements, ambiguity refusal, a tier that cannot emit links —
and it is the one proven capable of failing.

And it proves nothing about *real* bank data. It proves this pipeline holds
across 220 worlds from a generator whose rules are in this repo.

### Two numbers, never blended

An earlier version of this report printed a single combined match rate of
**99.2%**. Arithmetically correct, and misleading: 508 of the 529 scored links
are `order_entry`, where the gateway report hands over the join key in a column.
The real reconciliation — payout to bank credit, through a UTR buried in a
narration the bank may mangle, merge or split — scored **90.5%**. Blending them
buried a 9-point shortfall under an easy population. See D7 in `DECISIONS.md`.

---

## Quickstart

```bash
pip install -e .

python -m ledgerlock generate --profile default --seed 42   # build the world
python -m ledgerlock run --upto t3                          # reconcile
python -m ledgerlock eval                                   # score it
python -m ledgerlock queue                                  # the exception queue
python -m ledgerlock verify --profile all                   # assert every claim below
python -m ledgerlock sweep                                  # 60 worlds, is seed 42 lucky?
pytest -q
```

No API key needed: the model's answers are committed under `data/llm_cache/`.

<details>
<summary>All commands</summary>

```bash
python -m ledgerlock taxonomy                # the 12 exception codes and what they mean
python -m ledgerlock generate --profile smoke|default|scale --seed N
python -m ledgerlock inspect                 # read the sources back, show cash position
python -m ledgerlock run --upto t1           # deterministic tier only
python -m ledgerlock run --upto t2           # + rules, tolerances, bounded search
python -m ledgerlock run --upto t3           # + model-assisted residue
python -m ledgerlock run --upto t3 --llm off # skip the model entirely
python -m ledgerlock eval                    # -> data/out/report.md
python -m ledgerlock queue                   # -> data/out/queue.md
python -m ledgerlock verify --profile all|smoke|default|scale
python -m ledgerlock sweep --profiles smoke,default,scale --seeds 20 --upto t1|t2
```

To regenerate the model responses yourself: `cp .env.example .env`, add a
Gemini key from [AI Studio](https://aistudio.google.com/apikey), then
`run --upto t3 --llm live`.

</details>

| profile | orders | days | purpose |
|---|---|---|---|
| `smoke` | 50 | 28 | the stated 50-record bar; still exercises all twelve codes |
| `default` | 500 | 30 | the headline numbers |
| `scale` | 5,000 | 90 | throughput evidence — generates in ~5s |

Same profile + same seed gives a **byte-identical** dataset
(`test_same_seed_is_byte_identical`), and every published figure cites the
`manifest.json` carrying its seed.

---

## Why this problem

Three-way settlement reconciliation is **arithmetic and search, not language**.
A bank credit of ₹4,87,213.50 decomposing into 143 payments, minus fees, minus
two cross-cycle refunds, minus withheld tax, is a subset-sum problem with a
tolerance band.

That makes it a good test of engineering judgment, because the tempting
solution — hand the rows to a model and ask it to reconcile — is the wrong
instrument, and it fails in the most expensive way available: **confidently**.

---

## How it works

```
data/raw/                            data/truth/
  orders.csv          ERP              truth_links.csv        every true edge
  pg_entries.csv      gateway          truth_exceptions.csv   every injected fault
  bank_statement.csv  bank             manifest.json          seed + spec + counts
        |                                        |
        v                                        v
   io.loaders.load_sources              io.loaders.load_truth
        |                                        |
        v                                        |
   pipeline   T1  deterministic — no thresholds  |
              T2  rules, tolerances, subset-sum  |
              T3  model-assisted, no links       |
        |                                        |
        +----------------> eval <----------------+
                    match rate, false-match rate,
                    false alarms, correctly-refused
```

`load_sources` raises `TruthLeak` on any path containing a `truth` component.
The pipeline package therefore **cannot** read the answer key — that guard is
the mechanical reason these numbers are worth reading.

### The three tiers

Ordered by how much they can be trusted, not by how clever they are. Each
tier's residue is the next tier's only input.

**T1 — deterministic.** Defined by having *no thresholds*: existence checks,
exact key equality, exact arithmetic. No tolerance, no date window, no fuzzy
score, no model. A rule that has to decide "how close is close enough" is not a
T1 rule. That has a real cost, taken deliberately — T1 detects fee drift and
material mismatches but **refuses to name either**, because telling them apart
is a question about magnitude.

**T2 — rules and bounded search.** Where thresholds are allowed, and every one
is named, lives in `config.py`, and is reported alongside the decision it drove.
Splits rounding drift from material loss; recovers a mangled UTR by exact amount
and date; recovers merged credits by subset-sum; pairs cross-cycle refunds;
attributes a batch gap to the member breaks that caused it.

**T3 — model-assisted.** Classifies opaque adjustment narrations, suggests
candidates where amount alone cannot choose, and writes the plain-English
exception queue.

### The generator: prove, then break

1. Build a **perfectly reconciling** world.
2. **Assert** the settlement identity on every batch:
   `Σ(member entry nets) == payout == bank credit`. If step 1 has a bug,
   generation fails loudly instead of quietly emitting exceptions nobody
   intended.
3. Apply **injectors**, each corrupting exactly one dimension — an amount, a
   link, or a narration — and each recording a ground-truth row.

So every exception is there on purpose, and the answer key predates the matcher.
`World.claim()` stops two injectors touching one record, because a subject with
two faults is a subject no evaluator can score.

### `entry_type` semantics

The realism lives in the sign conventions:

| type | behaviour |
|---|---|
| `payment` | `net = gross − fee − tax`; fee from an MDR slab, tax = 18% GST **on the fee** |
| `refund` | `net = −amount`, and **the original MDR is not returned** |
| `chargeback` | `net = −(amount + ₹1,500 + GST on that fee)` |
| `adjustment` | arbitrary ±, **no `order_id`** — structurally unmatchable by order |
| `tds` | s.194-O at 0.1% of the batch's payment gross |
| `rolling_reserve_hold` / `_release` | 5% held, released 7 days later into a later payout |
| `settlement` | the aggregate row carrying the UTR; `net = −payout` |

Two behaviours are modelled rather than idealised away, because both break naive
matchers:

- **Refunds keep the fee.** A full reversal costs the merchant more than the sale
  earned. Any matcher assuming symmetry is wrong on every refund.
- **Negative cycles carry forward.** When a day's refunds exceed its inflows the
  payout would be negative; gateways do not claw money back, the deficit rolls
  into the next cycle. So a settlement batch is **not** always one T+2 bucket.

Money is an integer number of **paise** everywhere; `Decimal` appears only at the
fee boundary, rounded once, half-up, the way Indian processors round. There are
no floats in this project — a float would make the ±₹0.01 drift exceptions
accidental rather than deliberate, and quietly invalidate every number here.

---

## The exception taxonomy

`resolvable` is the column that matters. A case marked **none** is one the
pipeline is *supposed* to leave unresolved: correctly flagging it scores as a
success, and "resolving" it scores as a failure. Without that distinction, an
evaluator rewards a matcher for guessing.

| code | exception | resolvable | resolved at |
|---|---|---|---|
| E01 | Duplicate payment | partial | T1 |
| E02 | Missing in gateway | full | T1 |
| E03 | Orphan gateway entry | full | T1 |
| E04 | Fee rounding drift | full | T2 |
| E05 | Material amount mismatch | **none** | T2 → escalated |
| E06 | Unsettled at cut-off | full | T1 |
| E07 | Cross-cycle refund | full | T2 |
| E08 | Corrupted UTR in narration | full | T2 |
| E09 | Non-gateway inflow | full | T2 |
| E10 | Merged settlement credit | full | T2 (subset-sum) |
| E11 | Split settlement credit | full | T1 |
| E12 | Unexplained adjustment | **none** | T3 → classify only |

E05 and E12 are the honest-failure anchors. **A pipeline reporting 100% on this
dataset has lied**, and `test_open_cases_are_exactly_the_ones_that_should_be`
fails the build if that ever happens.

The dataset also carries **distractors that are not exceptions**: failed ERP
orders with no gateway entry (normal), salary and vendor debits, GST challans,
and benign adjustments that explain themselves. A matcher that flags those is
crying wolf.

### What is still open, and should be

22 cases escalated to a human, and 1 nobody could name:

- **5 duplicate payments** — resolvability `partial`. Whether to refund a second
  charge is a business decision, not a matcher's.
- **6 missing in gateway**, **4 orphan entries** — real breaks needing a person.
- **3 material mismatches** — resolvability `none`. Classified perfectly by T2,
  and every one still escalated. **Classification and resolution are different
  verbs.**
- **4 unexplained adjustments** — no order link, opaque narration; every
  one escalates. This is the honest one: **2 are correctly identified, 2 are
  false alarms** on benign narrations, and a third injected case was missed
  entirely. All three numbers are in the report; none was tuned away.

---

## How accuracy is measured

The harness makes three choices that each **lower** the headline number:

1. **`entry_settlement` links are not scored.** The gateway report supplies that
   column, so counting those 548 links would push the headline near 100% while
   proving nothing. The exclusion is printed in every report.
2. **A false match is counted separately from a miss**, and reported next to the
   match rate every time. In finance a gap gets investigated; a false match gets
   posted.
3. **A `resolvability = none` case is scored on whether the pipeline correctly
   refused to resolve it.** Auto-resolving one counts as a failure even though
   the records would appear to tie.

It also separates a **false alarm** from a **corroborating flag**: when the bank
merges two payouts into one credit, truth blames the bank line — but the second
settlement genuinely has no credit carrying its UTR, and saying so is correct
behaviour, not noise.

### Tier vs expectation

The taxonomy predicted which tier should resolve each code. The report prints
where reality differed:

```
E01 resolved at t1, expected t2 -- a cheaper mechanism sufficed
E06 resolved at t1, expected t2 -- a cheaper mechanism sufficed
E08 resolved at t2, expected t3 -- a cheaper mechanism sufficed
E11 resolved at t1, expected t2 -- a cheaper mechanism sufficed
```

---

## AI judgment: where the model is used, and where it is not

**T1 and T2 make no model calls at all.** They close 83 of 86 exceptions and
100% of the settlement-to-bank matching with a fee engine, exact joins, named
tolerances and a bounded search. At `scale`, **0.7% of records touch a model** —
a counter in the report, not a claim.

Generating the data with an LLM would have produced amounts that do not sum and
a ground truth nobody could trust. Matching with one would have produced
confident wrong links.

### The model tier shrank because of a rule I wrote instead

The taxonomy predicted **E08 (corrupted UTR) needed T3** — a mangled string looks
like a job for fuzzy matching. That was wrong. `HDFCN26O6O3OOOOI` is a bad
string to match, but the payout is an exact integer and the value date sits in a
known window. **Exact amount equality is far stronger evidence than any
edit-distance guess**, so R11 matches on amount and date, requires a *unique*
candidate, and escalates when two payouts of the same size could both fit.

### T3 emits no links. Ever.

Not at any confidence, with any evidence. It produces findings and suggestions
only. That turns the headline guarantee from an empirical result into a
structural one: **a tier that cannot create a link cannot create a false match.**
`test_t3_emits_no_links_ever` hands it a provider that flags everything and
asserts the link count does not move.

So the model decides whether a human *looks*. It never decides whether the books
balance — `_action_for` escalates any code whose resolvability is not `full`,
whatever the model says.

### Reproducible without an API key

Responses are content-addressed by `sha256(prompt + schema)` and committed under
`data/llm_cache/`. `--llm cached` is the default and needs no key. Verified with
`GEMINI_API_KEY` blanked: **0 live calls, identical output on all three
profiles.** `temperature=0` is not determinism and is not relied on.

And the cache is **checked, not trusted**. `--llm cached` fails loudly on an
unanswered prompt, *before writing an artefact*, because a silently incomplete
cache once passed here for days (F12) — and a keyless clone that quietly produces
different numbers is the one failure this project cannot tolerate.
`tests/test_cache_completeness.py` asserts 0 unanswered prompts across all three
shipped profiles against the real committed cache.

The provider is a **chain, not a model**, and that was empirical rather than
defensive: probing one free key for 90 seconds produced a 503 on
`gemini-3.7-flash`, a 503 on `gemini-3.6-flash`, a 429 on `gemini-3.5-flash`
after five calls, and a 404 on `gemini-2.5-flash`. Currently
`gemini-3.1-flash-lite`, pinned — never a `-latest` alias, so a cache regenerated
later hits the same weights.

### Where the model gets it wrong, unedited

T3 is the first tier here to produce false alarms, and they are reported rather
than tuned away. At `scale`, 6 disagreements across 59 adjustments — **every one
on a string the vocabulary marks as deliberately ambiguous**, and the model is
correct on all 23 unambiguous ones.

On the false alarm (`"CR ADJ - see support ticket 4471"`, flagged 3/3 at
confidence 1.00) **the model is arguably right and my label is wrong**: that text
says where to look, not what the money was for. I did not relabel the data and I
did not iterate the prompt — both would be overfitting to my own labels. Full
analysis in F10 of `DECISIONS.md`.

---

## How these claims are checked

Three layers, each doing something the others cannot.

**`pytest` — 154 tests.** Proves the parts behave: the generator is
deterministic, injection is surgical, subset-sum refuses ambiguity, T3 emits no
links, the committed cache covers every profile.

**`ledgerlock verify` — proves the *claims*.** The test suite never checked that
the sentences on this page were still true of the current checkout. This does,
per profile, exiting non-zero if one fails:

```
PASS  committed dataset matches the generator      profile default seed 42; byte-identical
PASS  no false matches asserted                    0 wrong links out of 5019 asserted
PASS  settlement -> bank fully matched             66/66 recovered
PASS  order -> gateway fully verified              4953/4953, precision 100%
PASS  no unresolvable case was auto-resolved       0 closed that must stay open
PASS  nothing left flagged-but-unnamed             0 findings with no code
PASS  model cache covers this dataset              0 prompts unanswered, 81 from cache
PASS  no live model call was needed                0 calls made
INFO  some exceptions correctly remain open        3 false alarms, 3 undetected, 794/797
```

That last row is deliberately **informational, not asserted**. A critical check
demanding zero false alarms would be a standing invitation to tune the data until
it passed.

**`ledgerlock sweep` — proves it was not one lucky dataset.** 220 worlds across
three seed ranges, reporting min/median/max and naming the worst seed so the
weakest case is reproducible rather than taken on trust. Exits non-zero if any
world produces a false match.

**CI — proves it on someone else's machine.** Tests across Python 3.11–3.13 on
Linux plus a Windows job, then a separate job that regenerates the committed
dataset and reconciles all three profiles end to end. The workflow references no
secrets and has a step that *fails if an API key is present*, because keyless
reproduction is a property being tested rather than assumed. The Windows job runs
with `PYTHONUTF8=0` to force legacy `cp1252` and catch any file I/O missing an
explicit encoding.

CI has already earned this twice. Its first run ever, in 40 seconds, found that
`httpx` and `python-dotenv` were imported but never declared — `pip install -e .`
in a clean venv was broken for everyone but me. Its second found that
"byte-identical" was true on Windows and false on Linux, because git stored the
CSVs as LF while `csv.writer` emitted CRLF. Both are in `DECISIONS.md` as F14
and F15.

---

## What broke

`DECISIONS.md` is a running log written as it happened — 18 decisions and 12
failures, each with the number attached. The three worth reading:

- **F7** — bank-fault rates scaled with order count, so at 5,000 orders 56% of
  payouts had a broken UTR path. T1 was being scored against fiction. Invisible
  at `default`, screamed at 10×.
- **F8** — a bank credit of **−₹11,186.80**. Injectors could push a thin batch's
  payout below zero; the clean world already handled deficits correctly but the
  injectors were never held to the same rule. Invisible at `default`, showed only
  at 0.1×.
- **F12** — the committed model cache was complete for `default` and **stale for
  the other two profiles**, and passed silently for days because `cached` mode
  treats a miss as normal. Would have shipped. Now guarded and regression-tested.
- **F14** — the very first CI run failed in 40 seconds: `httpx` and
  `python-dotenv` were imported but never declared, so `pip install -e .` in a
  clean venv was broken for everyone but me. Exactly what CI was added to catch.
- **F15** — the second run showed "byte-identical" was true on Windows and false
  on Linux: git stored the CSVs as LF while `csv.writer` emitted CRLF. The most
  load-bearing sentence in this README was quietly platform-specific, and I had
  "verified" it a dozen times — always on the same machine.

The through-line: four bugs were invisible because I only ran the profile I was
developing against, and one because I only ran in the environment I was
developing in. Same mistake, one axis over. Every measurement now runs all three
profiles, and CI runs on a machine that is not mine.

---

## Project layout

```
src/ledgerlock/
  config.py              every rate, tolerance and cycle parameter — one place
  domain/                money (integer paise), fees, models, taxonomy
  generate/              engine, injectors, narration vocabulary, writer
  io/loaders.py          reads data/raw only; raises TruthLeak otherwise
  pipeline/              tier1, tier2, tier3, subsetsum, views, controller
  llm/                   adapter, gemini provider, offline provider, prompts
  eval/                  metrics, report
  queueview.py           the exception queue
tests/                   154 tests across 8 files
data/raw/                the three sources
data/truth/              the answer key + reproducibility manifest
data/llm_cache/          committed model responses — no key needed to reproduce
```

---

## Status

- [x] Synthetic world with pre-injection identity proof, relational ground truth
- [x] 12-code taxonomy with a resolvability column
- [x] T1 deterministic — threshold-free by definition
- [x] T2 tolerances, cross-cycle lookback, amount+date recovery, bounded
      subset-sum, out-of-scope classification, batch-gap attribution
- [x] T3 model-assisted, emitting no links; committed response cache; offline
      provider; model chain
- [x] Eval harness: match rate, false-match rate, false alarms,
      correctly-refused accounting, per-tier arc, markdown report
- [x] Exception queue in plain English
- [x] Cache-completeness guard with a regression test over all three profiles
- [x] `ledgerlock verify` — asserts every published guarantee; CI on Linux
      (3.11–3.13) and Windows, keyless, with a no-API-key assertion
- [x] `ledgerlock sweep` — 220 independent worlds, 582k records, 0 false matches,
      with a mutation test proving the sweep can fail
- [ ] v2 taxonomy: a third narration class for "pointer, not a reason" (F10)
- [ ] Settlement Q&A over the attribution data R15 already computes
