# Known limitations (intentional, with reasons)

Prioritised so the decide -> verify -> act -> judge -> measure loop is complete and tested; the items below were left
out on purpose or could not be verified in the time.

## Model and decisions
* **The model has limited leverage and the live path is untested against the real service.** The OpenRouter and Anthropic
  clients were exercised only through fake transports and a local fake server (including 429/401 handling), never against
  the real APIs (no key in the build environment). Model outputs on a live run are non-deterministic, so a live run
  will not reproduce the committed mock `sample_run/` and its ITT estimate will differ. Free models are rate-limited and
  change over time; the breaker and `--live-call-cap` keep the run going rule-based when they refuse. Goal choice is rule-based with a hand-written priority list, not learned. This is deliberate (auditable,
  arm-symmetric), but it is not a claim that the priorities are optimal.
* **Goal fit is heuristic.** G05 requires a purchase-intent signal, G07 requires >=2 episodes played and 3-21 days idle,
  G06 "recent" = 30 days. These interpretations (all in `goals.py` and in each decision's `assumptions`) were chosen for
  proportionality, not tuned against outcomes. G07 achieves ~5% success in the simulator with or without the agent, so
  it is a poor use of a goal slot there; I kept it because the viewer intent is real, and report it honestly.
* **G10 and G11 are never selected** (no success event in the stream). I did not invent a proxy event.
* **Message copy is minimal:** three approved WhatsApp templates and two push patterns, Hinglish only, `dialect_name`
  is the code (`d3`) because the pack has no dialect names. No per-dialect copywriting.
* **No proactive action on dialect change** between visits; the next open sees the new dialect and acts then.
* **No viewer is messaged for goal-less or holdout states** and there is no "win-back after repeated failure"
  strategy; goal statistics only demote a goal that failed twice.

## Real-time and state
* **Single process; concurrency is modelled, not real.** Workers are a virtual pool (`heapq`); dependency timeouts are
  virtual waits. Measured compute is real but machine-dependent. Two decisions for the same viewer being in flight at
  once is not modelled (per-viewer serialisation is assumed, which a real deployment must provide).
* **No resume.** Viewer state is written durably and atomically, and idempotency keys are persisted in it, but a restarted
  process does not reload `viewer_state/` (datetime rehydration was not built). Writes deferred during a store outage are
  flushed on recovery within the same process.
* **In-memory event index** (fine for 5,000 viewers, not for 10M): see the scaling notes in `ARCHITECTURE.md`.
* **Unknown arm = holdout** (safe default); an arms file that omits viewers silently suppresses them.
* **Stale/holdout triggers still commit a goal** (a shadow goal with no action) so both arms are judged on the same
  definition of success. A goal can therefore be "open" for a viewer the agent never touched.
* **Withdrawal is conservative:** it blocks every language variant of the content, not just the named `title_id`.
* **Pages already shown cannot be un-shown.** A withdrawn title is removed from future pages immediately; a page that
  was live is superseded by a corrected one, but the earlier exposure stays in the record.

## Measurement
* **Statistical power is low.** ~800 viewers per arm at 10-20% base rates gives CIs of roughly +/-3.5 pp; per-goal rows
  are descriptive. Across the seeds I ran the ITT point estimate ranged from about -0.5 to +4.1 pp (only some intervals
  exclude 0). No CUPED/regression adjustment, no multiple-comparison correction, no sequential testing.
* **Only the first goal per viewer is analysed** in the headline; later goals (after a purchase or expiry) are judged
  and logged but not part of the ITT estimate.
* **Only replay-day viewers are in the experiment** and the horizon is the simulator's 14 days; no long-run or guardrail
  metrics (opt-out rate, churn, revenue per viewer). Opt-outs caused by messages are counted from the simulator report only.
* **The simulator-only paired check** (with vs without actions) is not available in production; it validates the
  ITT estimate here but proves nothing about the real world. The simulator's response model is itself an assumption.
* **Attribution is association.** `success_after_exposure` records exposure before success; only the holdout comparison
  speaks to causality.
* **Inconclusive** handling (window beyond the horizon, superseded goals, unmeasurable goals) is implemented and unit
  tested, but no goal was inconclusive in the supplied replay because all windows end inside the 14-day horizon.

## Data
* Duplicate/profile/dialect/clock handling is rule-based and counted in `final_report.md`; the rules were derived from
  the supplied pack and may need revisiting on a different one (e.g. other malformed `app_dialect` values).
* "30 days of history" means "never" claims (no play, no completion, never paid) are bounded by the event window; this
  is stated as an assumption on affected decisions with a confidence penalty, not resolved.
* The profile snapshot is the only source of consent; if it is missing or older than 72 hours no message is sent.
* Search queries are only scanned for injected instructions; they are not used as signals.

## Not done
No UI, no real queue/broker, no live model evaluation, no dashboards, no load test beyond the 400-trigger unit test
and the supplied replay's spikes, no per-viewer resume from disk.
