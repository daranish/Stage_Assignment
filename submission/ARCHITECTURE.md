# Architecture

## One picture

```
                    replay/triggers  live_events  control_events
                             \            |            /
                              v           v           v
   pack.py (load, dedupe, quarantine) -> EventIndex (per viewer, by received_at)
                                              |
        runtime.py  (harness: deliver_at order, worker pool, fault injector, control-event reactions)
                                              |
                                  ViewerAgent  (ONE per viewer; only writer of that viewer's goal/plan/history)
   trigger -> idempotency -> staleness -> facts.py ---> goals.py (eligibility, ONE goal) ---> model (advice only)
                                             |                                  |
                                             v                                  v
                                       planner.py (page / <=1 message / nothing) -> verifier.py (re-reads facts, can reject)
                                                                                         |
                                           state.py (viewer_state JSON, Ledger) <--- publish ---> actions.jsonl
                                                                                         |
   simulator -> outcome_events -> judge.py (attributable events only, rule version on the goal) -> state.learnings
                                                                                         |
                                                  impact.py (ITT vs holdout, pilot re-analysis) -> reports
```

The core is deterministic and rule-based. The model is a narrow, optional advisor. Everything that can hurt a viewer
(eligibility, dialect, consent, quiet hours, caps, copy, availability, holdout) is enforced by code that does not trust
the step that proposed the action.

## Ownership

`ViewerAgent` (agent.py) is the only code that commits, revises or closes a viewer's goal, and the only code that
writes that viewer's plan and history. `Runtime` routes events to agents; control-event handlers (title withdrawn, rule
corrected) call *into* the agent (`repair_page`, goal migration) rather than editing viewer state themselves. State
holds exactly one `active_goal`; a new goal is committed only after the previous one is closed (`succeeded`,
`superseded`, `expired_open`). `ledger` prevents two different components publishing the same action
(`trigger_event_id:type` key).

## Facts, sources, freshness (facts.py)

Each fact is `{value, source, as_of, age_s, refs, conflicts}`. Precedence is per fact and written in the module
docstring; nothing wins merely for being newest or largest.

| Fact | Rule |
|---|---|
| time | `received_at` is the only trusted clock. Event time = `received_at - (sent_at - occurred_at)` (the buffer gap is measured on one phone clock, so a wrong clock cancels out) |
| signup | profile `signup_at` (immutable) > `signup_completed` event; >24h disagreement kept as a conflict; no profile row -> event |
| subscription | later of {profile.updated_at, latest purchase/renewal/lapse `received_at`} wins; `never` can never override a paid-purchase event; expired `trial_active` is not trusted; duplicate profile rows: drop rows contradicted by events, then latest `updated_at` |
| dialect | trigger's `app_dialect` (what the app will request). Profile / campaign link / last `app_dialect_changed` listed as conflicts. `None`/`NA` -> last valid app setting, flagged *uncertain* (never used for messages). A dialect with no catalogue content (d7) -> abstain |
| consent | most restrictive across duplicate rows; any later `notification_opt_out` overrides; no profile row or a snapshot older than 72h -> no consent |

"Current enough to act on" is decided per use: home pages need only the trigger (dialect) and event-derived facts
(fresh); messages additionally need consent facts <=72h old *and* an unambiguous dialect; a cached fact (data store
down) can support a page but never a message.

## Planning (goals.py, planner.py)

* **Goal selection** is rule-based and identical for both arms: evaluate all 12 goals from facts, drop any with no
  measurable success signal (G10 null event; G11 `tv_app_open` is not in the stream), rank by a documented priority
  (G04, G01, G07, G12, G03, G02, G06, G05, G09, G08), demote goals that already failed twice for this viewer.
  Interpretations of under-specified rules are in the `goals.py` docstring and in each decision's `assumptions`.
* **Revision on situation change, not on a timer:** a purchase/renewal/lapse event re-evaluates the open goal
  (`still_valid`), closes it as `succeeded`/`superseded`, cancels pending messages that no longer make sense, and
  re-plans immediately (`kind: replan`). A corrected rule migrates only the open goals of that goal id.
* **Intervention:** a personalised home page for the dialect the app will request (up to 6 rows of 3-10 titles, one
  slug per page, hero chosen for the goal, availability required for the page's 120-minute life), and at most one
  message, only for goals where a nudge is proportionate, on a consented channel, at the viewer's usual open hour, never
  in quiet hours, inside the goal window. Otherwise: nothing, with a recorded reason.

## Model use (model.py)

Providers: `MockModel` (no key; default fallback), `OpenRouterModel` (`--model auto` when `OPENROUTER_API_KEY` is
set), optional `AnthropicModel`. `make_model` never raises for a missing key: it degrades to the mock. A live provider
may be asked for three jobs, each parsed defensively and each re-validated by code: **goal advice** (once per *new goal
commitment*, and at most every 6h for a viewer with no goal; picks only among rule-eligible goals), **hero pick**
(one title from a 5-title shortlist the planner already validated; an id outside the list is ignored) and **push /
in-app body** (used only if it passes `body_ok`, then the verifier's copy policy; otherwise template text; price-bearing
trial messages never use model text). The mock makes no hero/copy calls, so the no-key run equals the rule-based plan.
The model is never used to approve, never for facts. It receives a minimised candidate list and may only pick among rule-eligible goals. Output is parsed
defensively (`parse_advice`), retried once if malformed, never retried on timeout, and gated by a circuit breaker.
Because the holdout arm runs the same goal logic (with the same goal-advice call), the model cannot change who is measured
on what; hero/copy calls only shape actions, which holdout never takes. Fatal provider errors (401/402/403) disable the
provider for the run; 429/5xx/timeouts open the breaker; `--live-call-cap` bounds real calls on rate-limited free tiers.

## Verification (verifier.py)

Independent of the planner: it re-resolves the viewer's facts at publish time and re-checks holdout arm, duplicate,
goal measurability, rule version, goal eligibility, dialect (page and every title), availability for the page's whole
life and at message send time, page structure (rows <3 dropped, >6 rows, duplicate slug, row types), consent, quiet
hours, daily cap (ledger *and* delivered events), copy policy (length, forbidden phrases, prices only from
`pricing.json`, deeplink shape), approved template and catalogue-sourced variables, and instruction-like text in
outgoing copy. A failed global check cancels the decision; a failed action check drops only that action. Model
claims are re-derived: an "eligible" claim is accepted only if the rules reproduce it *and* every cited evidence ref
exists in the viewer's record (`check_model_claim`); the three `unsupported_claim` faults in the replay are logged as
incidents and ignored.

## Latency and budgets

`published_at = deliver_at + queue wait + measured compute + simulated dependency waits`. A pool of `workers`
(default 8) serves triggers in `deliver_at` order. Budgets are enforced per decision (`Budget`: 8 model calls, 20 tool
calls, both configurable; typical use is 0-2 model and 2-3 tool calls). Dependency waits are virtual (a 2 s model
timeout costs 2 s of accounted time without sleeping), which keeps replays fast and the accounting honest.
Optional work is shed first: when queue wait exceeds `shed_backlog_s` (10 s) model calls are skipped; stale triggers
exit before any planning. Measured on the supplied replay: p99 ~0.06 s, max ~2.0 s (a model timeout), nothing over 60 s.
A 400-triggers-in-one-second test with the model down finishes inside budget.

## Failure recovery

| Failure | Behaviour |
|---|---|
| model 503 / timeout / malformed / over budget | per-call timeout (2 s), one retry only for malformed, breaker opens for 60 s (later decisions skip the call at zero cost), rule-based goal, decision records `fallback_used` |
| data store slow/unavailable | per-read timeout (1 s) + one retry -> cached facts (page only, **no messages**, no new goal) -> generic dialect-correct popularity page, no goal -> no action. State writes are queued and flushed on recovery, not dropped |
| duplicate / late trigger | idempotency key = trigger `event_id`, persisted in viewer state; buffered opens (>600 s) are recorded as history with no action |
| title withdrawn | availability updated from that instant for all language variants; pending messages cancelled; live pages superseded by a verified corrected page |
| tool-call budget exhausted | the read loop stops and the agent falls back to the smaller action |

## Memory and audit

`viewer_state/<id>.json` holds facts with provenance, the active goal and plan, goal history (with rule version and
final judgement), compact decision/action indexes, outcomes, learnings, goal statistics and idempotency keys. Writes are
atomic (tmp + rename). `decisions.jsonl` carries the full per-decision record; `python -m oua audit --viewer U-...`
stitches decisions, action lifecycle, outcomes and incidents into one timeline (`--at` stops it at any moment).
Learnings feed back: goal statistics demote a goal that failed twice for the same viewer.

## Trust boundaries

* **Data is not instruction.** Catalogue text, titles, search queries and deeplinks are scanned at load
  (`INJECTION_RE`). Flagged titles are quarantined from pages and messages; the text is never passed to the model;
  only the dialect token of a deeplink is read (and validated against known dialects); outgoing copy is re-scanned.
* **Model output is a claim, not a fact.** It cannot grant eligibility, approve an action or add a fact.
* **Personal data stays local.** The mock provider sends nothing anywhere; the optional real provider receives only
  candidate goal ids and reasons.
* **The verifier is not a rubber stamp.** It shares only the goal-rule definitions with the planner; everything else it
  recomputes, and tests show it rejecting each class of violation.
* **Holdout is an invariant,** checked first by the verifier; holdout viewers carry a shadow goal for measurement but
  have no actions in the ledger.

## Measurement (judge.py, impact.py)

A goal is judged from events that are the viewer's own, after the goal started (by `received_at` *and* by estimated true
time, so a buffered event from before the goal cannot count), up to `min(deadline, observation horizon)`, using the rule
version recorded on the goal. Goals whose window outlives the observation horizon, are unmeasurable, or were
superseded are `inconclusive`, never silently `failed`. Impact is intention-to-treat: every triggered viewer in the arm
they were randomised to, including those the agent skipped, with Wald and bootstrap intervals, per-goal rows and
secondary outcomes over all viewers. A simulator-only paired check (with vs without actions, same seed) validates the
estimate and is labelled as not available in production.

## Scaling (discussion)

For 1,000 opens in a minute the design already holds (stateless-per-decision workers, O(viewer events) work, shed
optional model calls). For 10M viewers and 40 dialects: shard viewer state and the event index by viewer id; replace
the in-memory index with a keyed store plus a small per-viewer fact cache; precompute per-dialect candidate pools
(popularity, new releases) offline and refresh on catalogue events instead of scanning the dialect per decision;
make goal eligibility incremental (events update facts; the trigger reads them); keep control-event handling indexed
(`open_goals`, ledger-by-title) so a rule correction or withdrawal touches only affected viewers.
