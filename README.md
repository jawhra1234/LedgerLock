# LedgerLock

**A settlement reconciliation controller for a payment-gateway merchant.**

A merchant receives one lump credit in their bank account. It is net of MDR,
GST on that MDR, refunds, chargebacks, TDS under s.194-O and a rolling reserve.
Somewhere inside that single number are a few hundred individual orders.
LedgerLock closes that loop three ways — **ERP orders ↔ gateway ledger ↔ bank
statement** — reports its match rate against a known answer key, and hands back
an exception list it does not pretend to have solved.

> Status: **stage 1 of 3 complete.** The synthetic world, its relational ground
> truth and the exception taxonomy are built and tested. The reconciliation
> tiers and the scoring harness land next.

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
pytest -q
```

| profile | orders | days | purpose |
|---|---|---|---|
| `smoke` | 50 | 14 | the stated bar; still exercises all twelve codes |
| `default` | 500 | 30 | the headline number |
| `scale` | 5,000 | 90 | throughput evidence — generates in ~2s |

Same profile + same seed produces a **byte-identical** dataset
(`test_same_seed_is_byte_identical`), and every published figure cites the
`manifest.json` that carries its seed, so any number here is reproducible from a
clean clone.

## AI judgment: where the model is *not* used

Stage 1 contains **no model calls at all**. Generating synthetic finance data
with an LLM would produce amounts that do not sum, fees that do not follow a
slab, and a ground truth nobody could trust. It is a job for a fee engine and a
seeded PRNG.

The plan for the tiers follows the same reasoning:

- **T1 — deterministic.** Exact joins on `payment_id`, `order_id`, UTR. No model.
- **T2 — rules and search.** Tolerance bands, date windows, subset-sum over
  candidate settlements. No model. This is arithmetic; a model would be slower,
  costlier and less correct.
- **T3 — model-assisted, on the residue only.** Corrupted narrations (E08) and
  classifying opaque adjustments (E12): genuine natural-language problems.
  Output is a *suggestion with a confidence score and cited evidence*, never an
  auto-posted match. Below threshold it goes to the exception queue.

An LLM-proposed match is a suggestion pending review. In reconciliation a wrong
match is worse than no match, so the reported headline metric is not just match
rate — it is match rate **and false-match rate**.

## What is built

- [x] Domain model, integer-paise money, fee engine
- [x] Generator with pre-injection identity proof
- [x] Twelve injectors, one corruption dimension each
- [x] Relational ground truth + reproducibility manifest
- [x] Truth-leak guard, CLI, 26 tests
- [ ] T1 / T2 / T3 reconciliation tiers
- [ ] Eval harness: precision, recall, false-match rate, exception-classification accuracy
- [ ] Exception queue view

See `DECISIONS.md` for what broke on the way here.
