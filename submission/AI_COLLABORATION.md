# AI collaboration log

## Who did what

I (the candidate) set the direction and the architecture; AI tools helped with research and did most of the typing.

* **Research: ChatGPT and Perplexity.** I used both for background research while working out the problem (how
  experiments with a holdout are analysed, how to reason about attribution, how to structure an agent that decides,
  verifies and acts). I treated what they returned as input to my thinking, not as something to paste in.
* **Architecture: mine, discussed with Claude.** I decided the shape of the system: one agent per viewer that picks a
  single measurable goal, a planner that proposes and an independent verifier that can veto, a randomised holdout that
  runs the same goal logic with zero actions, judging only from attributable events, and an intention-to-treat impact
  estimate. I used Claude as a sounding board to pressure-test those choices and to work out details (fact precedence,
  fallbacks, what the model may and may not do).
* **Implementation: Claude (Claude Code, Sonnet 5.5).** Working from my architecture, the assignment PDF and the data
  pack, Claude wrote most of the code, the tests and the first drafts of these documents. I directed it, asked
  questions until I understood each part, and reviewed what it produced. It ran the code against the real pack and
  corrected its own mistakes (see below).
* **Model inside the agent.** By default the agent runs with a local deterministic mock model and sends nothing
  anywhere. If an `OPENROUTER_API_KEY` is set it uses OpenRouter (free models); only minimised data is sent (goal ids and
  short reasons, catalogue titles and genres, the viewer's top genres; no viewer ids or raw events). The committed
  `sample_run/` is the mock run. A small live test (30 capped calls) was run against OpenRouter with a free model.

Everything the reports claim was produced by running the code, not asserted.

## How output was checked

* **Ran everything.** Full replay on the real pack after each substantive change (`run-all`: ~2,800 decisions in ~8 s),
  then read the distributions (goals chosen, reasons for no action, check failures, fallbacks, latency), and individual
  decisions end to end, including one viewer's whole `audit` timeline.
* **Independent audit.** The simulator's own policy audit (`policy_problem_count`) is read in every run: 0 consent,
  quiet-hour, cap and holdout problems. `check_run.py` re-applies the policy rules without importing the agent.
* **Tests, and tests of the tests.** 53 unit/behaviour tests on a synthetic mini-pack that goes through the real loader.
  Then three deliberate mutations (quiet-hour/cap checks removed; idempotency removed; model-claim check removed): the
  first two made tests fail as expected; the third survived, which exposed a weak test, now strengthened.
* **Numbers cross-checked.** The pilot re-analysis was recomputed independently with a one-off script before it was put in
  the report (treatment 18.3% vs holdout 19.6%; served 40.1% vs others 17.7%); the ITT estimate was compared with a
  simulator-only paired run, and re-run on five other seeds to see the noise rather than quote one draw.

## Where the AI's first output was wrong, and what changed

1. **Goal G07 swallowed the replay.** First run: G07 ("resume series") was the goal for 1,417 of ~2,800 decisions
   because any viewer with any idle show qualified. Rejected as disproportionate; added resume intent (>=2 distinct
   episodes played, idle 3-21 days) -> 671.
2. **Dialects the README does not mention.** The README says `app_dialect` is the app setting, so the first
   implementation trusted it. Looking at the data showed `None`, `"NA"` and `d7` (no catalogue content) on 185 of
   2,769 triggers; the verifier caught a page proposed for dialect `NA`. Replaced with explicit semantics: unknown ->
   last valid setting flagged *uncertain* and never used for messages; unsupported -> abstain.
3. **A verifier false positive.** The verifier rejected legitimate model claims for goal G02 because an absence-based
   eligibility ("never completed an episode") cited no evidence. Fixed by citing the observed-absence evidence, rather than
   loosening the check.
4. **Idempotency-key collision.** Pages repaired after the title withdrawal used one shared key for every viewer, so the
   duplicate check blocked 11 of 12 repairs. The key now includes the viewer.
5. **Misleading text in the outputs.** The "learned" note said "deprioritise after repeated failure" on a first failure,
   and the data-issues table counted missing dialects as "unsupported". Both corrected after reading the generated reports.
6. **Mock model crashed on an empty candidate list** (viewers with no eligible goal); fixed, and the model is now
   consulted for such viewers at most every six hours so the replay's injected faults on them are exercised.
7. **Giving the agent a real model (OpenRouter).** I wanted a live model with a safe fallback when no key is set.
   `auto` mode falls back to the mock without a key, and the mock makes no extra calls, so the no-key run is unchanged
   (re-checked: same 2,794 decisions, +1.5 pp, 0 violations). The model may only pick from validated shortlists and its
   copy must pass code checks before the verifier sees it; the holdout arm gets the same goal-advice call. The first
   live test exposed three real problems: the free model was a reasoning model that used its whole token budget thinking
   and returned nothing, the goal-advice call still used the old 2-second timeout, and one slow call took a decision to
   55 s. All three were fixed (more tokens, low reasoning effort and shorter prompts; the provider's timeout; a per-call
   timeout bounded by the time left in the decision). One of the two free models I configured returned HTTP 403.

## Suggestions I chose not to accept

* **Letting the model choose goals only in the agent arm** (or using model confidence to pick between goals). Rejected: the
  holdout would then be measured against a different definition of success, breaking the intention-to-treat comparison.
  The holdout runs the same goal logic and the same goal-advice call; it just gets no actions.
* **Reporting uplift as served vs everyone else** because it is the easiest number and matches the pilot report.
  Rejected; it is the pilot report's mistake. ITT is the headline, the naive number is shown only to explain the bias.
* **Treating an offline-buffered app open as a live session** (using `received_at - occurred_at`). Rejected: phone clocks
  are wrong, so the age is the buffer gap `sent_at - occurred_at`, which cancels clock error.
* **Tuning goal priorities against simulator outcomes** (e.g. avoiding G07 because it rarely succeeds). Rejected as
  overfitting to a simulator the evaluators will re-seed; the limitation is documented instead.
* **Picking "newest source wins" for subscription status.** Rejected in favour of per-fact precedence (a stale `never`
  must not override a purchase event; an expired trial must not be trusted).
* **Letting the verifier share the planner's computed facts.** Rejected: the verifier re-reads facts itself so it can disagree.

## My own notes

<!-- Add anything specific only you can say: what you asked ChatGPT/Perplexity, what you changed after reading the
code, what you tried yourself. Delete this comment when done. -->
