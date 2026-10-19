# Final report

Run: simulator seed `oua-practice`, model provider `mock`, 2769 triggers, 1746 viewers with an agent.

## 1. What the system did

| | Count |
|---|---|
| Decisions logged | 2794 |
| Trigger decisions, agent arm | 1354 (published an action: 860, deliberate no-action: 494) |
| Trigger decisions, holdout arm (shadow goal, **zero actions**) | 1325 |
| Duplicate trigger deliveries suppressed | 90 |
| Actions published | home_page: 867, in_app_message: 55, push: 160, whatsapp: 112 |
| Messages cancelled before sending | 5 |
| Goals judged | {'failed': 1330, 'succeeded': 271} |
| Re-plans after a situation change | 14 |

Why the agent did nothing (agent arm):

- stale trigger: 191
- page already live / nothing to change: 175
- no goal (thin evidence): 77
- dialect unsupported/unknown: 51

## 2. Latency and reliability

p50 0.052s, p95 0.054s, p99 0.057s, max 2.003s; **2690 of 2690 decisions within 60s, 0 over 300s**. Peak minute: 79 triggers. Model calls per decision <= 2 (limit 8), tool calls <= 3 (limit 20). See `latency_report.json`.

## 3. Surprises and how the system reacted

| Surprise | Detected how | Response | Evidence |
|---|---|---|---|
| Offline-buffered app opens | sent_at - occurred_at > 600s | recorded as history, no action | 191 stale triggers, all NO_ACTION |
| Model claims eligibility without evidence | verifier re-derives eligibility and cited refs | claim rejected, incident logged, rule-based goal used | 3 unsupported claims logged in `incidents.jsonl` |
| Title withdrawn | control event | availability updated instantly; pending messages cancelled; live pages replaced | 3 messages cancelled, 11 pages replaced |
| G03 success rule corrected | control event | rule v2; only open G03 goals migrated | 0 open G03 goals migrated; 761 other open goals untouched |
| Model provider outage | 503s / timeouts | per-call timeout, circuit breaker, rule-based goal | breaker opened 6 times; 18 decisions used a model fallback |
| Data store degraded | read timeouts | retry once, then cached facts (no messages), then generic dialect-correct page, then no action; writes queued | 17 reads timed out, 7 decisions used a store fallback, 16 writes flushed on recovery |
| Purchase arrives after a decision | live event | goal judged, pending messages cancelled, viewer re-planned | 14 re-plans; see `decisions.jsonl` (kind `replan`) |
| Injected instruction in a catalogue description | text scan at load | row quarantined, logged, never obeyed | 1 catalogue rows quarantined; 0 mass messages |
| Traffic spike | arrival rate / queue wait | parallel workers; model skipped when queue wait > 10s | max queue wait 0.000s |

## 4. Data issues found and how they were handled

| Issue | Size | Handling |
|---|---|---|
| Duplicate events (same event_id) in history | 2176 duplicate lines | dedupe by event_id at load; first received kept |
| Duplicate trigger deliveries | 90 redeliveries | idempotency key = trigger event_id; the repeat creates a DUPLICATE_SUPPRESSED decision and no action |
| Offline-buffered (stale) app opens | 432 triggers buffered >600s | age = sent_at - occurred_at (one clock, so skew cancels); stale => recorded as history, no action |
| Wrong phone clocks | 78 triggers where received_at and sent_at differ by more than 10 min | received_at is the only ordering/freshness clock; buffer age used for event time |
| Profile snapshot: two rows for one viewer | 30 viewers | drop rows contradicted by purchase events, take latest updated_at; conflict kept in the decision; consent = most restrictive |
| No profile row (new viewers) | 103 triggered viewers | facts built from signup_completed; consent unknown => no push/WhatsApp |
| Dialect: app setting vs profile vs campaign link | 75 triggers with three different dialects | app setting decides (what the app will request); conflicts listed in the decision |
| Dialect missing / "NA" / unsupported (d7) | 185 triggers (missing: 93, "NA": 26, no catalogue content e.g. d7: 66) | missing/NA => last valid app setting, flagged uncertain, never used for messages; d7 has no catalogue => abstain |
| Language variants of one title | 514 content_slugs with 2 title_ids | dedupe by content_slug; pick the variant matching app language; withdrawal blocks all variants |
| Availability windows | 103 titles with an end, 54 not yet released | availability checked at publish time AND for the page TTL; message titles checked at send time |
| Instruction injected in catalogue text | 1 catalogue rows, 0 search queries | treated as data: quarantined from content, logged, never obeyed; outgoing copy re-scanned |
| Unmeasurable goals | G10 (event null), G11 (tv_app_open not in stream) | never selected; listed as "considered, not selectable" |
| Under-specified goal rules | G06 "recently", G07 "unfinished/3 days", G05 breadth, G12 "before the trigger" | interpretations in goals.py docstring and in each decision's assumptions |
| Pilot report compares served vs everyone else | 40.1% vs 17.7% | recomputed with the randomised arms (see impact_report.md) |

## 5. Does the agent help?

Intention-to-treat on first goals: agent arm 17.7% vs holdout 16.2%, difference **+1.5 pp** (95% CI -2.2 to +5.2; n = 789 + 801). Full analysis, per-goal rows and the pilot re-analysis are in `impact_report.md`.

The simulator-only paired check gives +2.90 pp (95% CI +1.73 to +4.06).

The pilot report's 2.3x headline is a selection artefact; its own randomised arms show -1.3 pp. Be sceptical of any number here that is not an ITT comparison.

## 6. Policy checks on this run

The simulator audits every action independently of this system (`sim/simulator_report.json`).

| Check | Result |
|---|---|
| Policy problems (consent / quiet hours / frequency cap / holdout) | **0** |
| Actions on holdout viewers | 0 |
| Pushes/WhatsApp blocked for missing consent | 0 |
| WhatsApp rejected (unapproved template) | 0 |
| Home pages with dropped rows / rejected hero | 0 / 0 |
| Opt-outs caused by our messages | 0 |
| Pages accepted / shown personalised | 867 / 772 |
| Pushes: delivered / undeliverable device token | 136 / 24 (token validity is hidden by the platform) |
