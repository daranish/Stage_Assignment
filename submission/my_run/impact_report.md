# Impact report

Simulator seed: `oua-practice`. Primary comparison = **intention-to-treat**: every triggered viewer is analysed in the arm they were randomised to (`replay/arms.csv`), whether or not the agent acted on them.

## 1. Headline (this replay)

Success = the viewer's first committed goal was judged `succeeded` from attributable events; `inconclusive` goals are excluded and counted below. The goal is chosen by the same code for both arms, before any action, so the two arms are judged on the same definition of success.

| Comparison | Viewers | Agent arm | Holdout arm | Difference | 95% CI (Wald) |
|---|---|---|---|---|---|
| **All goals (ITT)** | 1590 | 17.7% (140/789) | 16.2% (130/801) | +1.5 pp | [-2.2 pp, +5.2 pp] |
| goal G01 | 114 | 32.8% (20/61) | 28.3% (15/53) | +4.5 pp | [-12.4 pp, +21.4 pp] |
| goal G02 | 407 | 13.2% (28/212) | 10.8% (21/195) | +2.4 pp | [-3.9 pp, +8.7 pp] |
| goal G03 | - | - | - | - | - |
| goal G04 | 2 | 0.0% (0/1) | 0.0% (0/1) | +0.0 pp | [+0.0 pp, +0.0 pp] |
| goal G05 | 138 | 8.0% (6/75) | 6.3% (4/63) | +1.7 pp | [-6.9 pp, +10.3 pp] |
| goal G06 | 78 | 7.5% (3/40) | 10.5% (4/38) | -3.0 pp | [-15.7 pp, +9.7 pp] |
| goal G07 | 425 | 4.7% (9/193) | 4.7% (11/232) | -0.1 pp | [-4.1 pp, +4.0 pp] |
| goal G08 | 219 | 31.5% (34/108) | 27.9% (31/111) | +3.6 pp | [-8.5 pp, +15.7 pp] |
| goal G09 | 65 | 36.1% (13/36) | 34.5% (10/29) | +1.6 pp | [-21.7 pp, +25.0 pp] |
| goal G12 | 141 | 43.5% (27/62) | 43.0% (34/79) | +0.5 pp | [-16.0 pp, +17.0 pp] |

Bootstrap 95% CI for the headline difference: [-2.2 pp, +5.3 pp]; two-sided p = 0.421.

Inconclusive first goals (excluded above): {}.

### Secondary outcomes (all triggered viewers, with or without a goal)

| Outcome | Viewers | Agent arm | Holdout arm | Difference | 95% CI |
|---|---|---|---|---|---|
| any play within 7 days of first decision | 1746 | 67.6% (583/863) | 64.9% (573/883) | +2.7 pp | [-1.8 pp, +7.1 pp] |
| paid purchase within 14 days | 1746 | 3.8% (33/863) | 4.1% (36/883) | -0.3 pp | [-2.1 pp, +1.6 pp] |

### Simulator-only oracle check (not available in production)

The simulator is deterministic per (seed, viewer), so the same agent-arm viewers can be replayed with and without the agent's actions. Paired over 794 agent-arm viewers with a conclusive first goal: success 17.8% with actions vs 14.9% without; paired effect **+2.90 pp** (95% CI +1.73 pp to +4.06 pp). This checks that the ITT estimate above is pointing in the right direction; it can only be computed because the world is simulated.

## 2. Why "served vs everyone else" misleads

| Comparison on this replay | Viewers | Group A | Group B | Difference |
|---|---|---|---|---|
| served vs all others (the pilot report's method) | 1590 | 20.0% | 14.8% | +5.2 pp |
| ITT, agent arm vs holdout arm | 1590 | 17.7% | 16.2% | +1.5 pp |

"Served" is decided by the agent *after* it sees who is active, eligible, consented and reachable. Those viewers differ from the rest before any action happens. Mean app sessions in the 14 days before the replay (history only, untouched by the agent):

- served (agent arm): 5.31 (n=671)
- agent arm, not served: 3.72 (n=192)
- holdout arm: 5.12 (n=883)

The viewers who are served are the ones the agent could act on, so the served-vs-rest gap mixes the effect of the agent with who was selected. Only the randomised arms break that link: assignment happened before the agent saw anyone, so the arms differ in nothing but the agent (in expectation). The holdout arm includes viewers the agent chose to skip, which is exactly what keeps the comparison fair.

## 3. Re-analysis of the earlier pilot

The internal report compared 217 served viewers with everyone else (40.1% vs 17.7%, +22.4 pp). Recomputed from `pilot_outcomes.csv`:

| Comparison | Viewers | Group A | Group B | Difference | 95% CI |
|---|---|---|---|---|---|
| served vs all others (report) | 3891 | 40.1% (87/217) | 17.7% (649/3674) | +22.4 pp | [+15.8 pp, +29.1 pp] |
| **treatment arm vs holdout arm (ITT)** | 3891 | 18.3% (349/1912) | 19.6% (387/1979) | -1.3 pp | [-3.8 pp, +1.2 pp] |
| ITT, goal G02 | 360 | 19.2% (34/177) | 19.7% (36/183) | -0.5 pp | [-8.6 pp, +7.7 pp] |
| ITT, goal G04 | 129 | 23.8% (15/63) | 22.7% (15/66) | +1.1 pp | [-13.5 pp, +15.7 pp] |
| ITT, goal G05 | 2118 | 1.2% (13/1045) | 0.6% (6/1073) | +0.7 pp | [-0.1 pp, +1.5 pp] |
| ITT, goal G06 | 331 | 0.6% (1/157) | 2.9% (5/174) | -2.2 pp | [-5.0 pp, +0.5 pp] |
| ITT, goal G09 | 953 | 60.9% (286/470) | 67.3% (325/483) | -6.4 pp | [-12.5 pp, -0.4 pp] |

ITT bootstrap 95% CI: [-3.8 pp, +1.3 pp].

Mean app sessions during the pilot window: served 6.18, treatment-but-unserved 3.99, holdout 4.27. Served viewers are, by construction, viewers who opened the app, so the "2.3x uplift" is mostly the difference between visitors and non-visitors. Against holdout viewers who also visited at least once (1970 viewers, 19.6% success) the served viewers' 40.1% is +20.4 pp.

**Conclusion: the pilot showed no demonstrable uplift (ITT -1.3 pp). The recommendation to roll out on the strength of the 2.3x headline is not supported.**

## 4. Caveats

- Goal success rates are low-powered per goal; per-goal rows are descriptive, not individually significant unless the interval excludes 0.
- The simulator draws one world per seed; evaluators rerun with another seed and a second replay day, so treat point estimates as one draw.
- Only viewers who triggered on the replay day are in the experiment; effects on viewers who never open the app are out of scope.
- Inconclusive goals (window beyond the 14-day observation horizon, or superseded) are excluded symmetrically in both arms.

Simulator policy-problem count for this run: **0** (consent, quiet hours, frequency cap, holdout violations).
