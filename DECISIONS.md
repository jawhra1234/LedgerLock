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
