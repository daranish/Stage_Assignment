# One User, One Agent

A per-viewer agent for the STAGE OTT streaming app. When a viewer opens the app, one agent for that viewer:

1. **Decides**: picks one measurable goal (for example "first episode played within 7 days") and either one proportionate action or nothing, from evidence.
2. **Verifies**: an independent verifier re-reads the facts and can veto the plan.
3. **Acts**: publishes a personalised home page and at most one message, inside a 60 s / 8 model call / 20 tool call budget, degrading safely when a dependency fails.
4. **Judges**: decides afterwards whether the goal succeeded, failed or was inconclusive, using only events attributable to that viewer.
5. **Measures**: compares the agent arm with a randomised holdout arm to estimate real impact.

Pure Python standard library (3.9+). No dependencies. **No API key needed**: without one the agent runs fully
rule-based; with an OpenRouter key it also uses a model (see [Model options](#model-options)).

---

## Quick start

You need Python 3.9+ and the unzipped data pack (the folder that contains `config/`, `data/`, `replay/` and `simulator/`).

```bash
# 1. Run everything: replay -> act -> simulate outcomes -> judge -> measure impact -> reports  (~30 seconds)
python run_replay.py --pack /path/to/one_user_one_agent_pack --out sample_run

# 2. Read the results
#    sample_run/final_report.md    what happened, surprises, data issues, policy audit
#    sample_run/impact_report.md   agent vs holdout, per goal, pilot re-analysis

# 3. Independently audit the published actions against the policy checklist (does not import the agent)
python check_run.py --pack /path/to/one_user_one_agent_pack --run sample_run

# 4. Run the tests (53, no data pack or network needed)
python -m unittest discover -s tests -v
```

Windows example (from the `submission` folder, pack next to it):

```
python run_replay.py --pack "..\one_user_one_agent_data_pack (4)\one_user_one_agent_pack" --out sample_run
```

`--pack` can also be set with the `OUA_PACK` environment variable. `sample_run/` is the output of a rules-only run
(`--model mock`); running the command above regenerates it.

Expected end of the output for the rules-only run:

```
ITT: agent 17.7% (n=789) vs holdout 16.2% (n=801)  diff +1.5 pp  CI [-2.2, +5.2]
simulator policy problems: 0
latency p99 0.057s  max 2.003s  over 60s: 0
```

### See what the agent did for one viewer

```bash
cd src
python -m oua audit --out ../sample_run --viewer U-02219      # add --at <ISO time> to stop the timeline at a moment
```

This prints one timeline: what the agent knew (with sources and freshness), the goal it chose, the action it took or
why it did nothing, the checks, and the judged outcome.

---

## Input

The agent reads the data pack. Nothing is hard-coded to the supplied replay day: the replay day, observation horizon and
rule versions are derived from the data.

| File | Role |
|---|---|
| `replay/triggers.jsonl` | The input events: `app_open` / `login_success`, each starts one decision |
| `replay/arms.csv` | Pre-randomised arm per viewer (`agent` or `holdout`). The agent never assigns arms; a viewer not listed is treated as holdout (safe default) |
| `replay/live_events.jsonl` | Events arriving during the replay (purchases, opt-outs, ...) that can revise a plan |
| `replay/control_events.jsonl` | Title withdrawn, goal rule corrected, dependency outages |
| `replay/model_faults.jsonl` | Faults injected into the model (timeout, malformed JSON, unsupported claim) |
| `data/events.jsonl`, `profiles_snapshot.csv`, `catalogue.csv`, `episodes.csv` | History, profiles, catalogue |
| `config/goals.json`, `interventions.json`, `pricing.json` | Goals G01-G12, the action menu and limits, prices |

Using another replay day or arms file:

```bash
python run_replay.py --pack <pack> --replay-dir <folder with triggers/live/control files> --arms <arms.csv> --out other_run
```

To try a custom scenario, add a trigger line to `triggers.jsonl` (copy an existing line), add the viewer to `arms.csv`,
and make sure they have history in `events.jsonl` and a row in `profiles_snapshot.csv`.

## Output (`sample_run/`)

| File | What it is |
|---|---|
| `decisions.jsonl` | Every decision: goal + deadline + success signal, action or reason for none, dialect and conflicts, evidence with freshness, confidence with reason, assumptions, checks passed/failed, model/tool calls, fallback used, time taken |
| `actions.jsonl` | What was published (the simulator's input) |
| `actions_log.jsonl` | Lifecycle of each action: published / cancelled (why) / superseded |
| `outcomes.jsonl` | Each goal judged `succeeded` / `failed` / `inconclusive`, with evidence and what was learned |
| `viewer_state/<id>.json` | Durable per-viewer state: facts with provenance, active goal and plan, goal history, outcomes, learnings |
| `incidents.jsonl` | Surprises: withdrawn title, corrected rule, outages, unsupported model claims |
| `latency_report.json` | Latency percentiles, budgets used, per-outage behaviour, model provider and call stats |
| `impact_report.md` | Intention-to-treat comparison vs holdout, per-goal rows, pilot re-analysis |
| `final_report.md` | Summary, surprises, data issues, policy audit |
| `sim/` | Simulator output (`outcome_events.jsonl`, `simulator_report.json`) |

---

## How the agent works

```
trigger -> idempotency -> staleness -> facts -> ONE goal -> plan (page + <=1 message, or nothing) -> verify -> publish
                                                  ^ model advises only
later: simulator events -> judge (attributable events, goal's rule version) -> impact (agent vs holdout)
```

**Goal.** All 12 goals are evaluated from the viewer's facts. Goals with no measurable success signal (G10, G11) are
dropped. The rest are ranked by a fixed priority (G04, G01, G07, G12, G03, G02, G06, G05, G09, G08), a goal that already
failed twice for this viewer is demoted, and the top one becomes the viewer's single active goal. The rules are identical
for both arms.

**Action.** A personalised home page (hero plus up to 6 rows of 3-10 titles, one dialect, available for the page's whole
120-minute life) and, only for goals where a nudge is proportionate (G01, G03, G04, G06, G07, G12), at most one message
(WhatsApp template, push or in-app) on a consented channel at the viewer's usual hour, never in quiet hours (22:00-08:00
IST), at most one per day. Otherwise it does nothing and records why.

**Verification.** The verifier does not trust the planner. It re-reads the facts itself and re-checks holdout arm,
duplicates, goal eligibility and rule version, dialect, title availability, page structure, consent, quiet hours, daily
cap, copy policy (length, forbidden phrases, prices only from `pricing.json`), approved templates and injection-like text.
A failed global check cancels the decision; a failed action check drops only that action.

**Revision.** A purchase, renewal or lapse re-evaluates the open goal immediately (not on a timer). A withdrawn title
is removed from future pages and pending messages, and live pages are replaced by a verified corrected page. A corrected
rule migrates only the open goals of that goal id.

**Failure handling.** Model down or malformed: rule-based goal and template copy. Data store slow: cached facts, page only,
no messages. Duplicate or stale trigger: no action. Budget exhausted: the smaller action.

**Holdout.** Holdout viewers run the same goal logic (a "shadow" goal, so success means the same thing in both arms) but
receive zero actions. The verifier blocks them first.

**Measurement.** A goal is judged from the viewer's own events after the goal started, up to
`min(deadline, observation horizon)`, using the rule version recorded on the goal. The headline impact number is
intention-to-treat: every triggered viewer in the arm they were randomised to, with Wald and bootstrap confidence intervals.

Full design, with the fact-precedence table and trust boundaries: [ARCHITECTURE.md](ARCHITECTURE.md).

---

## Model options

`--model auto` (default) chooses for you:

| Situation | Provider | Behaviour |
|---|---|---|
| `OPENROUTER_API_KEY` set (environment or `.env`) | OpenRouter (free models work) | Model advises goals and does two bounded creative jobs |
| No key | Deterministic mock | No network, no extra calls: the rule-based agent. Nothing breaks |
| Key set but wrong, out of credit or rate-limited | Falls back per call | 401/402/403 disables the provider for the run; 429/5xx/timeouts open a circuit breaker; the rule-based goal and template copy are used |

```bash
cp .env.example .env     # Windows: copy .env.example .env   then put your key in .env (never commit it)
python run_replay.py --pack <pack> --out live_run --live-call-cap 100
```

| Option | Meaning |
|---|---|
| `--model auto \| mock \| openrouter \| anthropic` | Provider. `mock` forces the offline path |
| `--live-call-cap N` | Max real model calls per run (0 = unlimited). Free tiers are slow and rate-limited; after the cap the agent continues rule-based |
| `OUA_MODEL` (env/.env) | Model id; a comma list gives OpenRouter-side fallbacks. Default `meta-llama/llama-3.3-70b-instruct:free`; see openrouter.ai/models?max_price=0 for current free models |
| `OUA_MAX_TOKENS` | Token allowance per call (default 1500; reasoning models need headroom) |

**What the model may do, and nothing more:**

1. *Goal advice*: choose among goals the rules already found eligible. Its claim is re-derived by the verifier.
2. *Hero pick*: choose one title from a 5-title shortlist that is already dialect-correct and available. Anything else is ignored.
3. *Message line*: write one short Hinglish body for a push / in-app message. It is used only if it passes the copy policy;
   otherwise the template text is used. Trial-ending messages carrying prices always use the fixed template.

It cannot supply facts, grant eligibility or approve an action. Only goal ids, short reasons, catalogue titles/genres and
the viewer's top genres are sent (no viewer ids or raw events). The mock makes none of the hero/copy calls, so a no-key run
is identical to the pure rule-based one. Holdout viewers get the same goal-advice call as agent-arm viewers and no
hero/copy calls (those only shape actions). Each decision records `model_extra` (what was asked, whether it was used) and
`latency_report.json` records provider, live calls and per-task outcomes.

**What was tested:** offline with fake transports and a local fake OpenRouter server (including 429 and 401), and live
on a 30-call-capped replay with a free OpenRouter model (0 audit violations). A full live replay (~2,000 calls) was not
done because free-tier speed and rate limits make it impractical. Free reasoning models can take 4-18 s per call.
Live output is non-deterministic, so a live run will not reproduce the rules-only numbers exactly.

---

## Results (what was measured, not a promise)

Rules-only run, one simulator draw (seed `oua-practice`):

| | |
|---|---|
| Decisions / actions | 2,794 / 1,199 |
| ITT (first-goal success), agent vs holdout | 17.7% (n=789) vs 16.2% (n=801): **+1.5 pp, 95% CI [-2.2, +5.2]** |
| Simulator policy problems | 0 |
| Latency | p99 about 0.06 s, max about 2 s, none over 60 s |

Across other simulator seeds the ITT difference ranged from about -0.5 to +4.1 pp (only some intervals exclude 0); the
simulator-only paired check (same viewers with and without actions, unavailable in production) was +1.6 to +3.0 pp.
Read: a small positive effect that about 800 viewers per arm can barely resolve. The pilot's 2.3x headline comes from
comparing served viewers to everyone else, which is selection bias; its randomised arms show -1.3 pp.

---

## Repository layout

```
run_replay.py            one-command entry point
check_run.py             independent audit of a run against the policy checklist
.env.example             template for the OpenRouter key
src/oua/                 pack.py facts.py goals.py planner.py verifier.py model.py agent.py runtime.py
                         state.py judge.py impact.py report.py cli.py config.py util.py
tests/                   test_agent.py test_measurement.py test_model.py fixtures.py (synthetic mini-pack)
sample_run/              output of a rules-only run
ARCHITECTURE.md          design, fact precedence, planning, verification, failure recovery, scaling notes
AI_COLLABORATION.md      how AI tools were used, what was wrong, what was rejected
KNOWN_LIMITATIONS.md     what is intentionally not done, with reasons
```

Other CLI flags: `--no-oracle` (skip the simulator-only paired check), `--limit N` (first N triggers), `--seed S`
(simulator seed), `--workers N`, `--max-model-calls 8`, `--max-tool-calls 20`. Equivalent module form:
`cd src && python -m oua run-all --pack <pack> --out ../sample_run`.

## Limitations

Short version: statistical power is low, goal priorities are hand-written rather than learned, agent state lives in one
process (no resume from disk), the live-model path has been tested only lightly on a free model, and arm assignment for
brand-new viewers is consumed from `arms.csv` rather than implemented. The complete, prioritised list is in
[KNOWN_LIMITATIONS.md](KNOWN_LIMITATIONS.md).
