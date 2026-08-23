# LedgerLock

**A settlement reconciliation controller for a payment-gateway merchant.**

A merchant receives one lump credit in their bank account. It is net of MDR,
GST on that MDR, refunds, chargebacks, TDS under s.194-O and a rolling reserve.
Somewhere inside that single number are a few hundred individual orders.
LedgerLock closes that loop three ways — **ERP orders ↔ gateway ledger ↔ bank
statement** — reports its match rate against a known answer key, and hands back
an exception list it does not pretend to have solved.

> Status: **T1 + T2 + scoring harness complete.** 100% settlement-to-bank
> matching with zero false matches at every dataset size, 83 of 86 exceptions
> classified, and the 3 it cannot touch reported as undetected. T3
> (model-assisted) is scoped to what T2 genuinely leaves behind.

---

## Why this problem

Three-way settlement reconciliation is arithmetic and search, not language. A
bank credit of ₹4,87,213.50 decomposing into 143 payments, minus fees, minus
two cross-cycle refunds, minus withheld tax is a subset-sum problem with a
tolerance band. That makes it a good test of engineering judgment, because the
tempting solution — hand the rows to a model and ask it to reconcile — is the
wrong instrument, and it fails in the most expensive way available: confidently.

## Design

```
data/raw/                          data/truth/
  orders.csv          ERP            truth_links.csv        every true edge
  pg_entries.csv      gateway        truth_exceptions.csv   every injected fault
  bank_statement.csv  bank           manifest.json          seed + spec + counts
        |                                      |
        v                                      v
   io.loaders.load_sources            io.loaders.load_truth
        |                                      |
        v                                      |
   pipeline  T1 deterministic                  |
             T2 rules / subset-sum             |
             T3 model-assisted residue         |
        |                                      |
        +---------------> eval <---------------+
                   precision, recall,
                   false-match rate
```

`load_sources` raises `TruthLeak` on any path containing a `truth` component.
The pipeline package therefore *cannot* read the answer key — that guard is the
mechanical reason the accuracy numbers below are worth reading.

### The generator: prove, then break

1. Build a **perfectly reconciling** world.
2. **Assert** the settlement identity on every batch:
   `Σ(member entry nets) == payout == bank credit`.
   If step 1 has a bug, generation fails loudly instead of quietly emitting
   exceptions nobody intended.
3. Apply **injectors**, each corrupting exactly one dimension — an amount, a
   link, or a narration — and each recording a ground-truth row.

So every exception in the dataset is there on purpose, and the answer key was
written before the matcher existed.

### `entry_type` semantics

The realism lives in the sign conventions. Each type behaves as the gateway
actually behaves:

| type | behaviour |
|---|---|
| `payment` | `net = gross − fee − tax`; fee from an MDR slab, tax = 18% GST **on the fee** |
| `refund` | `net = −amount`, and **the original MDR is not returned** |
| `chargeback` | `net = −(amount + ₹1,500 + GST on that fee)` |
| `adjustment` | arbitrary ±, **no `order_id`** — structurally unmatchable by order |
| `tds` | s.194-O at 0.1% of the batch's payment gross |
| `rolling_reserve_hold` / `_release` | 5% held, released 7 days later into a later payout |
| `settlement` | the aggregate row carrying the UTR; `net = −payout` |

Two behaviours are modelled rather than idealised away, because both break
naive matchers:

- **Refunds keep the fee.** A full reversal costs the merchant more than the
  sale earned. Any matcher assuming symmetry is wrong on every refund.
- **Negative cycles carry forward.** When a day's refunds exceed its inflows the
  payout would be negative; gateways do not claw money back, the deficit rolls
  into the next cycle. So a settlement batch is *not* always one T+2 bucket.

Money is an integer number of **paise** everywhere. `Decimal` appears only at
the fee boundary, rounded once, half-up, the way Indian processors round. There
are no floats in this project — a float would make the ±₹0.01 drift exceptions
accidental rather than deliberate, and quietly invalidate every number here.

## The exception taxonomy

`resolvable` is the column that matters. A case marked **none** is one the
pipeline is *supposed* to leave unresolved: correctly flagging it counts as a
success, and "resolving" it counts as a false match. Without that distinction an
evaluator rewards a matcher for guessing.

| code | exception | resolvable | expected tier |
|---|---|---|---|
| E01 | Duplicate payment | partial | T2 |
| E02 | Missing in gateway | full | T1 |
| E03 | Orphan gateway entry | full | T1 |
| E04 | Fee rounding drift | full | T2 |
| E05 | Material amount mismatch | **none** | T2 → escalate |
| E06 | Unsettled at cut-off | full | T2 |
| E07 | Cross-cycle refund | full | T2 |
| E08 | Corrupted UTR in narration | full | **T3** |
| E09 | Non-gateway inflow | full | T2 |
| E10 | Merged settlement credit | full | T2 (subset-sum) |
| E11 | Split settlement credit | full | T2 (subset-sum) |
| E12 | Unexplained adjustment | **none** | T3 → classify only |

E05 and E12 are the honest-failure anchors. A pipeline that reports 100% on this
dataset has lied, and the eval harness is built to catch exactly that.

The dataset also carries **distractors that are not exceptions**: failed ERP
orders with no gateway entry (normal), salary and vendor debits, GST challans.
A matcher that flags those is crying wolf, and `test_failed_orders_are_noise_not_exceptions`
pins it down.

## Run it

```bash
pip install -e .

python -m ledgerlock taxonomy                      # the codes, and what they mean
python -m ledgerlock generate --profile default --seed 42
python -m ledgerlock inspect                       # read the sources back
python -m ledgerlock run --upto t1                 # deterministic tier only
python -m ledgerlock run --upto t2                 # + rules, tolerances, search
python -m ledgerlock eval                          # score  -> data/out/report.md
pytest -q
```

| profile | orders | days | purpose |
|---|---|---|---|
| `smoke` | 50 | 28 | the stated bar; still exercises all twelve codes |
| `default` | 500 | 30 | the headline number |
| `scale` | 5,000 | 90 | throughput evidence — generates in ~2s |

Same profile + same seed produces a **byte-identical** dataset
(`test_same_seed_is_byte_identical`), and every published figure cites the
`manifest.json` that carries its seed, so any number here is reproducible from a
clean clone.

## Results

Profile `default`, seed 42. 1,125 source records, 86 injected exceptions.
Each tier measured separately on the same dataset via `run --upto t1|t2`, so the
contribution of each is evidence rather than a claim.

| metric | T1 | T2 |
|---|---|---|
| settlement -> bank matching | 90.5% (19/21) | **100.0%** (21/21) |
| order -> gateway verification | 100.0% (508/508) | **100.0%** (508/508) |
| **false matches asserted** | **0** | **0** |
| **false alarms on clean records** | **0** | **0** |
| exceptions classified | 44/86 | **83/86** |
| unnamed residue | 24 | **0** |
| undetected | 20 | **3** (E12 only) |

Two numbers, never blended. `order_entry` is 508 links where the gateway hands
over the join key in a column; averaging it in would report ~99% and hide the
part that is actually hard. See D7 in `DECISIONS.md`.

The same pipeline, unchanged, at three sizes -- so the result is a property of
the matcher, not of one convenient dataset:

| profile | records | T1 | T2 | false matches |
|---|---|---|---|---|
| `smoke` | 189 | 88.9% | **100%** | 0 |
| `default` | 1,125 | 90.5% | **100%** | 0 |
| `scale` | 10,441 | 89.4% | **100%** | 0 |

### What is still open, and should be

18 cases escalated to a human, and 3 nobody can close:

- **5 duplicate payments** -- resolvability `partial`. Whether to refund a
  second charge is a business decision, not a matcher's.
- **6 missing in gateway**, **4 orphan entries** -- real breaks needing a person.
- **3 material mismatches** -- resolvability `none`. Classified perfectly by T2,
  and every one still escalated. Classification and resolution are different
  verbs.
- **3 unexplained adjustments** -- no order link, opaque narration. Reported as
  undetected rather than omitted.

A pipeline reporting nothing open on this dataset would be lying, and
`test_open_cases_are_exactly_the_ones_that_should_be` fails the build if that
ever happens.

### Tier vs expectation

The taxonomy predicted which tier should resolve each code. Four beat it:

```
E01 resolved at t1, expected t2 -- a cheaper mechanism sufficed
E06 resolved at t1, expected t2
E08 resolved at t2, expected t3
E11 resolved at t1, expected t2
```

E08 is the interesting one, and it shrank the model tier -- see below.

## AI judgment: where the model is *not* used

**Nothing in T1 or T2 makes a model call.** 83 of 86 exceptions and 100% of the
settlement-to-bank matching are closed by a fee engine, exact joins, named
tolerances and a bounded search.

Generating the data with an LLM would have produced amounts that do not sum and
a ground truth nobody could trust. Matching with one would have produced
confident wrong links, the most expensive output available in finance.

### The model tier shrank because of a rule I wrote instead

The taxonomy predicted **E08 (corrupted UTR) needs T3** -- a mangled string
looks like a job for fuzzy or semantic matching. That was wrong.
`HDFCN26O6O3OOOOI` is a bad string to match, but the payout is an exact integer
and the value date sits inside a known window. **Exact amount equality is far
stronger evidence than any edit-distance guess**, so R11 matches on amount and
date, requires a *unique* candidate, and escalates when two payouts of the same
size could both fit.

So T3's remit is now:

- **E12** unexplained adjustments -- genuinely narration semantics, 3 of 86 cases
- **Ambiguous R11 cases** -- two same-sized payouts in one window, where amount
  cannot discriminate and narration is the only tiebreak

That is where a model earns its place, and nowhere else. **3 of 86 exceptions**,
on records a deterministic pipeline has already proved it cannot resolve. An
LLM-proposed match will be a suggestion with a confidence and cited evidence,
never an auto-posted link -- and the harness already counts an auto-resolved
unresolvable case as a failure.

## What is built

- [x] Domain model, integer-paise money, fee engine
- [x] Generator with pre-injection identity proof
- [x] Twelve injectors, one corruption dimension each
- [x] Relational ground truth + reproducibility manifest
- [x] Truth-leak guard, CLI, 98 tests
- [x] T1 deterministic tier -- threshold-free by definition
- [x] Eval harness: precision, recall, false-match rate, false-alarm and
      correctly-refused accounting, per-tier arc, markdown report
- [x] T2: tolerance bands, cross-cycle lookback, amount+date recovery,
      bounded subset-sum, out-of-scope classification, batch-gap attribution
- [ ] T3 model-assisted residue -- E12 classification and ambiguous candidates
- [ ] Exception queue view

See `DECISIONS.md` for what broke on the way here.
