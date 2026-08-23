# Decisions and failures

A running log, written as it happened rather than reconstructed afterwards.
Every entry states what went wrong, what the evidence was, and what changed.

---

## Day 1 — 22 Aug 2026

### D1. Ground truth is relational and physically separated

**Decision.** Truth lives in `data/truth/` as two normalised long-format CSVs
(`truth_links`, `truth_exceptions`), never as extra columns on the source files.
`io.loaders.load_sources` raises `TruthLeak` if asked to read any path with a
`truth` component.

**Why.** Embedding labels in the sources makes accidental leakage almost
inevitable — one careless `df.columns` and the matcher is reading the answer.
Long format also means a merged settlement is expressible as two rows sharing a
`line_id`, with no schema change. The guard is what makes the eventual accuracy
number defensible rather than a claim.

### D2. Prove the clean world before breaking it

**Decision.** Generate a perfectly reconciling world, `assert` the settlement
identity on every batch, and only then run injectors.

**Why.** Without step 2, a generator bug becomes an unlabelled exception in the
dataset — the pipeline would be blamed for a fault we created by accident. The
manifest records `clean_identity_verified: true` for the same reason.

### D3. Integer paise, no floats anywhere

**Decision.** All money is `int` paise; `Decimal` only at the fee boundary,
rounded once with `ROUND_HALF_UP`.

**Why.** The dataset deliberately contains ±₹0.01–0.50 rounding-drift
exceptions. With floats those would be indistinguishable from binary
representation error, so the E04 label would be a lie. Half-up rather than
banker's rounding because that is what Indian processors use on fee lines.

### D4. Refunds do not return the MDR; negative cycles carry forward

**Decision.** Model both, rather than idealising them away.

**Why.** They are the two behaviours that break naive matchers, so a dataset
without them would flatter the pipeline. See F2 for how the second one arrived.

### D5. No LLM in the generator

**Decision.** Synthetic data comes from a fee engine and a seeded PRNG.

**Why.** A model would produce amounts that do not sum and fees that do not
follow a slab — and worse, a ground truth nobody could trust. The whole project
is about verification; starting with unverifiable inputs would be
self-defeating. This is the first of the "where I chose not to use one" answers.

### D6. Scope cut: multi-currency

**Decision.** Dropped international payments and their separate fee slabs from
v1. Recorded rather than silently omitted.

**Why.** Roughly doubles the fee engine's complexity for one extra exception
class. The twelve existing codes already exercise every tier of the pipeline.

---

## Failures

### F1. LLM-first matching was rejected on the design table, not in code

Original instinct was to pass batches of records to a model and ask it to
reconcile. Rejected before writing it: the hard part of this problem is
subset-sum over a tolerance band, which a model does badly and expensively,
and its failure mode is a *confident wrong match* — the worst possible outcome
in finance. Cost of the wrong choice: a rewrite in week two. Cost of catching
it early: one hour of design.

**Outcome:** tiered architecture. Deterministic first, rules second, model only
on the residue, and never auto-posting.

### F2. `RuntimeError: non-positive payout for 2026-06-05: -12304198`

**What broke.** First generator run died immediately. On a thin day in the
smoke profile, one full refund of a ₹60k+ order plus a chargeback exceeded the
day's inflows, so the computed payout was negative and my `assert payout > 0`
guard fired.

**Diagnosis.** The guard was right to fire and my model of the world was wrong.
I had assumed one T+2 bucket == one settlement. Real gateways do not withdraw
money from a merchant's bank when a cycle goes negative; the deficit carries
into the next cycle, so a settlement batch can span several days of traffic.

**Fix.** `_settle` now keeps a `pending` list and only pays out when the running
total turns positive, rolling the deficit forward otherwise.

**Why it mattered.** This is now one of the harder cases in the dataset rather
than a crash — and any matcher that assumes "settlement == one day's bucket"
will fail on those batches, which is exactly the kind of assumption this project
exists to catch.

### F3. E10 silently injected zero occurrences

**What broke.** The manifest showed `E10 merged settlement: 0` while every other
code injected. Nothing raised.

**Diagnosis.** Injectors picked candidates with `rng.sample(...)` and then
`continue`d if another injector had already claimed one. With only six
settlements in the smoke profile, the single sampled candidate collided with an
E08 line and the injector simply produced nothing.

**Fix.** Replaced sample-and-give-up with `_pick()`: shuffle the pool, skip
contested candidates, keep going until `n` succeed or the pool is exhausted.

**Why it mattered.** This is the failure mode that scares me most in this
project — not a crash, but a **silent** gap. A code that never injects means the
pipeline is never tested on that branch, and its reported accuracy would look
*better* for it. `test_every_exception_code_is_exercised` now fails the build
rather than trusting me to read a table.

### F4. The gateway ledger contradicted itself

**What broke.** `test_settlement_rows_mirror_their_payout` failed: settlement
`STL_0002`'s summary row said ₹3,50,150.64 while its members summed to
₹3,58,300.64 — off by ₹8,150.

**Diagnosis.** Injectors that move real money (E01 duplicate, E03 orphan, E12
adjustment) called `_bump_bank`, which updated the bank credit and the internal
`Settlement.payout` but not the gateway's own `settlement` ledger row. Three
views of the same payout, only two kept in step.

**Fix.** `_bump_bank` now updates all three.

**Why it mattered.** That stale row would have looked to the pipeline like an
amount break we never injected — an unlabelled exception, which is precisely
what D2 exists to prevent. Found by a test written *before* the matcher, which
is the only reason it was cheap.

---

## Day 2 -- 23 Aug 2026

### D7. No blended match rate

**Decision.** The report never prints one combined match rate. It prints
`settlement -> bank matching` and `order -> gateway verification` separately.

**Why.** The first version printed **99.2%**. That number was arithmetically
correct and deeply misleading: 508 of the 530 scored links are `order_entry`,
where the gateway report hands over the join key in a column. The real
reconciliation -- gateway payout to bank credit, through a UTR buried in a
narration that the bank may mangle, merge or split -- scored **81.8%**. A
blended figure would have buried an 18-point shortfall under an easy
population. Caught it before it reached a report, not after.

### D8. Tier 1 is defined by having no thresholds

**Decision.** T1 admits existence checks, exact key equality and exact
arithmetic. No tolerance, no date window, no fuzzy score, no model. If a rule
has to decide "how close is close enough", it is not a T1 rule.

**Why.** It gives the tier boundary a testable definition instead of a vibe,
and it is what makes T1's output trustworthy enough to post without review.
It also has a real cost, accepted deliberately: T1 detects E04 rounding drift
and E05 material mismatch but **refuses to name either**, because telling them
apart is a question about magnitude. Reporting 28 findings as "flagged, not yet
explained" is more honest than guessing a code to look complete.

### D9. `entry_settlement` links are excluded from scoring

**Decision.** The pipeline verifies that settlement batches tie, but the
`entry -> settlement` edge is not counted in the match rate.

**Why.** The gateway report supplies `settlement_id` as a column. Counting
those ~560 links would have taken the headline close to 100% while proving
nothing. The exclusion is printed in the report rather than left implicit.

### D10. A corroborating flag is not a false alarm

**Decision.** Findings on records ground truth considers clean are split into
`false_alarms` and `corroborating`, by checking whether the record shares a true
edge with something actually broken.

**Why.** When the bank merges two payouts into one credit, truth blames the
bank line -- but the *second* settlement genuinely has no credit carrying its
UTR, and saying so is correct behaviour. Scoring it as a false alarm would
punish a pipeline for being thorough. Four such flags on this dataset; all four
trace to a genuinely broken neighbour.

---

## Baseline: T1 alone, profile `default`, seed 42

| metric | value |
|---|---|
| settlement -> bank matching | 90.5% (19/21) |
| order -> gateway verification | 100.0% (508/508) |
| **false matches** | **0** |
| **false alarms** | **0** |
| exceptions detected | 66/86 |
| ...classified | 44 |
| ...flagged but unnamed | 24 |
| ...undetected | 20 |

Holds across scales with the tier unchanged: `smoke` 83.3% (15/18), `default`
90.5% (19/21), `scale` 89.4% (59/66), zero false matches at every size.

Fully classified at T1: E01, E02, E03, E06, E11. Detected but deliberately
unnamed: E04, E05, E08, E09, E10. Invisible to T1 and reported as such: E07
(needs cross-cycle lookback), E12 (needs narration semantics).

Recorded before T2 exists, so the improvement T2 delivers is measurable rather
than asserted.

> **Note on a moved number.** An earlier version of this table read 81.8%
> (18/22). Nothing about T1 changed; the *dataset* did, when F7 recalibrated
> bank-fault rates. The number moved because a miscalibrated dataset was
> injecting twice as many broken UTR paths as a real bank produces. Recorded
> here rather than quietly overwritten, because a baseline that silently
> improves is worthless as a baseline.

---

## Failures, day 2

### F5. Ground truth would have punished a correct pipeline

**What broke.** Before writing a line of the matcher, working through how E06
would be scored: the injector labelled a *sample* of unsettled entries (10 of
28). A pipeline that correctly found all 28 would have been charged with 18
false alarms.

**Diagnosis.** E06 and E07 are not injected faults, they are *observations* --
being unsettled, or being a refund that settled later than its payment, is a
fact about the data. Sampling an observation leaves identical records
half-labelled, and the answer key becomes wrong rather than incomplete.

**Fix.** Both now label the entire population. E06 went from 10 to 28, E07 from
8 to 17.

**Why it mattered.** This is the subtlest failure so far: nothing crashed, no
test failed, and the pipeline would have looked worse than it was. Ground truth
is code and it needs the same suspicion as everything else.

### F6. The pipeline had to import from the generator

**What broke.** `pipeline/tier1.py` needed the fee engine to recompute fees, and
the fee engine lived in `generate/fees.py`. The reconciler was importing from
the thing that fabricated its input.

**Diagnosis.** Wrong home from the start. An MDR slab and the GST rate are
domain facts, not generation concerns -- the README already claimed the pipeline
recomputes fees "with these same functions", which only reads as a strength if
they live somewhere neutral.

**Fix.** `git mv` to `domain/fees.py`, imports rewired, 26 tests still green.
Two minutes on day 2; it would have been an ugly untangle in week two.

### F7. Bank faults scaled with order count, so the scale profile was fiction

**What broke.** Verifying the pushed repo from a clean clone, the three profiles
disagreed badly on the one metric that matters:

```
smoke    (50 orders)     settlement -> bank   71.4%
default  (500 orders)    settlement -> bank   81.8%
scale    (5,000 orders)  settlement -> bank   53.9%
```

T1 does not get worse with volume, so the data was wrong.

**Diagnosis.** Every injection rate was expressed per *order*. But E08 (mangled
UTR), E09, E10 (merged credit) and E11 (split credit) are **payout-level**
faults, and payouts scale with business days, not with order count. At 5,000
orders that produced 20 corrupted UTRs and 15 merged credits across just 63
settlements -- **56% of payouts with a broken UTR path**. A bank that mangles
more than half a merchant's payouts is not a bank. T1 was being scored against
fiction, and the 35 missed links were exactly 20 + 15.

**Fix.** Added an explicit `Basis` to each code: order-level rates stay
per-order, payout-level rates are measured against settlement count. Then
widened the `smoke` window from 14 to 28 days, because with only 6 payouts the
">=1 occurrence of every code" floor put a bank fault on a third of them by
construction -- coverage and realism were in direct conflict at that size.

After: broken UTR path is 12% / 10% / 11% across smoke / default / scale, and
the profiles finally agree -- 83.3%, 90.5%, 89.4%, zero false matches at every
size.

**Why it mattered.** Two ways this would have cost me. The scale profile is the
throughput evidence, and it was carrying a match rate that read as a failing
grade for reasons unrelated to the matcher. Worse, it would have *flattered* T2:
subset-sum would have had 15 merged pairs to recover instead of a realistic
2-3, so T2's improvement would have looked far larger than it deserved.

Two tests now pin it: `test_bank_faults_stay_plausible_at_every_scale` fails the
build above a 15% ceiling, and `test_bank_fault_counts_do_not_track_order_count`
asserts that ten times the orders does not mean ten times the clumsy narrations.

**The real lesson.** I only found this because I re-ran all three profiles from
a clean clone instead of trusting the one profile I had been developing against.
The bug was invisible at `default` and only screamed at 10x.

---

## Day 3 -- 23 Aug 2026 (evening): tier 2

### D11. Confidence and action are separated, and resolvability outranks both

**Decision.** A rule states how strong its evidence is. A single function,
`tier2._action_for`, decides what may be done about it. A code whose
resolvability is not FULL never auto-resolves, at any confidence.

**Why.** It keeps the judgement in one auditable place instead of scattered
through eleven rules, and it makes the project's central distinction
mechanical: E05 is now **classified perfectly, 3 of 3**, and every one of them
still goes to a human. Classification and resolution are different verbs.
`test_classified_does_not_mean_resolved` pins it.

### D12. Absorbed rounding drifts stay visible

**Decision.** The 15 fee drifts inside the Rs 0.50 tolerance are auto-resolved
*and* still listed, each with its amount.

**Why.** Absorbing them silently is how a small systematic overcharge lives
forever. A controller that quietly nets out Rs 3.95 a cycle is indistinguishable
from one that has a bug. Slightly noisier report, materially more trustworthy.

### D13. `--upto` exists so each tier's contribution is measured, not asserted

**Decision.** `run --upto t1|t2`, and `test_tier1.py` is pinned to
`upto=Tier.T1` forever.

**Why.** Without it, adding T2 would have silently changed what the T1 tests
measured and the baseline would have stopped being evidence. Three tests failed
the moment T2 landed, which is exactly what should have happened. It also means
the arc below can be reproduced by anyone on one dataset, in two commands.

### D14. Subset sum refuses to choose

**Decision.** Exact equality only, subset size capped at 3, pool capped at 12,
date-coherent candidates, truncation reported -- and if two different subsets
both hit the target, it returns nothing and says so.

**Why.** Subset sum with a free hand is a machine for producing plausible
answers: with enough candidates *some* subset sums to almost anything. This is
the only component in the project capable of inventing a confident wrong link,
so it is the only one tested purely on hand-written integers, with no dataset
involved at all.

---

## The arc, on one dataset

Profile `default`, seed 42. Reproduce with
`run --upto t1` then `run --upto t2`.

| metric | T1 | T2 | predicted for T2 |
|---|---|---|---|
| settlement -> bank matching | 90.5% (19/21) | **100.0%** (21/21) | 100% |
| order -> gateway verification | 100.0% | 100.0% | -- |
| **false matches** | **0** | **0** | 0 |
| **false alarms** | **0** | **0** | -- |
| exceptions classified | 44/86 | **83/86** | ~80/86 |
| unnamed residue | 24 | **0** | 0-2 |
| undetected | 20 | **3** | 3 (E12 only) |

Holds at every size, tier unchanged:

| profile | records | T1 | T2 | false matches |
|---|---|---|---|---|
| `smoke` | 189 | 88.9% | **100%** | 0 |
| `default` | 1,125 | 90.5% | **100%** | 0 |
| `scale` | 10,441 | 89.4% | **100%** | 0 |

The predictions were written into the plan before the code existed and are
recorded here unedited. All five held.

**What is still open, and should be:** 18 escalated cases -- 5 duplicate
payments (resolvability `partial`), 6 missing in gateway, 4 orphans, 3 material
mismatches (resolvability `none`) -- plus 3 unexplained adjustments that no
deterministic rule can touch. A pipeline reporting nothing open on this dataset
would be lying, and `test_open_cases_are_exactly_the_ones_that_should_be`
fails the build if that ever happens.

---

## Failures, day 3

### F8. A bank credit of minus Rs 11,186.80

**What broke.** T2 hit 100% on `default` and `scale` but stalled at 94.4% on
`smoke`, leaving one settlement unmatched with `no bank credit carries UTR
HDFCN26062600016`. The UTR was right there in the narration.

**Diagnosis.** The line's `credit` column held **-1118680**. A negative credit
is not a thing. Tracing back: injectors that take money out of a batch --
E02 removing a payment, E12 posting a negative adjustment -- called
`_bump_bank` with a negative delta, and nothing stopped that from driving a thin
batch's payout below zero. The clean world already handled this correctly
(F2: deficits carry forward, gateways do not claw back), but the *injectors*
were never held to the same rule. The reconciler then filtered the line out via
`credit > 0` and honestly reported that no credit carried the UTR -- correct
behaviour on impossible data.

**Fix.** `_payout_after()` guards both injectors: E02 only picks payments whose
removal leaves the batch positive, E12 flips sign when a batch has no headroom.
Plus `_verify_signs()` now asserts after injection that no credit or debit is
negative and no payout is non-positive -- the same prove-it-loudly treatment the
clean world already got.

**Why it mattered.** Third time now that a bug was invisible on the profile I
was developing against and only showed up at a different size. F7 needed 10x
to surface; this one needed 0.1x. The lesson is the same and I have stopped
treating it as a coincidence: **every measurement runs on all three profiles,
every time.**

### F9. `>/dev/null` was reporting false failures

Chained verification commands kept returning exit 1 while the same commands
succeeded when run alone. Not a code bug: under msys on Windows, `rich` writing
to `/dev/null` fails and typer surfaces it as a non-zero exit. Wasted about ten
minutes chasing a phantom. Redirect to a temp file instead. Recorded because a
tooling artefact that looks exactly like a real failure is worth knowing about
before it happens during a demo.
