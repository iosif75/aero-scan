# Aero-Scan

Flight anomaly detection on open ADS-B data, centred on Barcelona–El Prat. **Deterministic rules decide; a local LLM explains** (LLM stage: milestone 2).

The detector watches each aircraft as a once-a-minute poller would see it (the budget of a free [OpenSky](https://opensky-network.org) account) and flags emergency transponder codes, rapid descents from altitude, turnbacks and holding. Every rule is a pure function of a short window of position reports, so it is unit-tested, deterministic, and identical live and in evaluation.

The monitored region is 39.3–43.3°N, 0–5°E: El Prat, Girona, Reus, Sabadell and Palma, 20 square degrees, which is one OpenSky credit per poll.

## Results

Rules are evaluated with the **transponder code hidden**, to measure what they catch from movement alone, and with every flight replayed as 60-second polls. Thresholds were set on the dev split; the numbers below are the held-out **test split**.

### Emergencies (worldwide)

Barcelona alone saw too few emergencies to measure recall (11 of 832 in the reference set within 250 km), so detection is measured on the full worldwide set.

| Metric | Test split |
| --- | --- |
| Squawk-7700 flights | 397 |
| Detected from trajectory alone, all flights | 24% |
| Detected from trajectory alone, flights that diverted | 52% |
| Detected before the 7700 squawk appeared | 17 flights |
| Median detection time | 170 s after the squawk |
| Emergency-squawk rule, squawk visible | 100% |

Recall by reported problem (categories with ≥ 9 flights):

| Problem | Flights | Detected |
| --- | --- | --- |
| Cabin pressure | 9 | 7 (78%) |
| Smoke / burning smell / fire | 11 | 8 (73%) |
| Technical | 11 | 6 (55%) |
| Medical | 40 | 22 (55%) |
| Engine | 10 | 5 (50%) |
| Unclear | 104 | 31 (30%) |
| No public report | 186 | 7 (4%) |

**How to read this.** Many 7700 squawks leave no trace in the trajectory: the crew declares, carries on and lands as planned. Flights with no news or Aviation Herald report are mostly of that kind, hence 4%. Where the emergency changes the flight (a pressurisation descent, a return or a diversion), the rules catch about half or more. This is why the squawk rule stays in the live system: trajectory rules add early warning and context; they don't replace it.

### False alarms on ordinary traffic

| Dataset | Test split |
| --- | --- |
| **Barcelona region, 12 days of 2025** | 110 of 18,698 flights flagged (0.59%), 1 alert per ~78 flight-hours |
| of which descents from cruise | 7,569 flights, 58 `rapid_descent` alerts |
| Dublin approaches, 2019 | 73 of 8,756 flights (0.83%) |
| Normal part of the 7700 flights | 10 alerts in 289 flight-hours |

In operational terms, the Barcelona region sees about 3,100 flights a day, so **about 18 flights a day would be flagged in error**. That is the number to bring down: milestone 2 puts an LLM triage step behind the rules, and its evaluation must show it cuts these without losing detections.

Emergency squawks seen in the 12 Barcelona days: 4 flights. Three held the code long enough to be reported by the squawk rule (two 7600 radio failures, one 7700) and are excluded from the false alarms; none showed a trajectory anomaly. The fourth was a 7700 lasting about a second, which the persistence filter correctly ignored.

Full output, including the dev split: [`eval/results/report.md`](eval/results/report.md).

### Known limitations

- **Turnbacks remain the main false alarm**: 67 of the 125 Barcelona test alerts and most Dublin ones, typically vectoring and approach manoeuvres above 10,000 ft. Approach context (distance and bearing to the nearest airport) is the obvious next rule input.
- **Steep normal descents**: at −3,750 ft/min the descent rule still flags about 0.3% of ordinary Barcelona flights.
- Ground truth for emergencies is "squawked 7700", so 7700 onset is the reference time. Some emergencies start earlier, which is why alerts up to 10 minutes before onset count as detections.
- The Barcelona false-alarm count is an upper bound: an alert on an ordinary day may occasionally be a real, unreported event.

## Alert triage (milestone 2)

The rules above are tuned to keep false alarms down, which costs detections. The alternative tested here: run **looser** rules and put a learned triage step behind them that sorts each alert into *real* or *routine*, plus a likely cause.

Training data comes from every alert of the loose rules (`aeroscan/triage/dataset.py`): 153 real and 2,952 routine alerts in dev, 159 and 2,995 in test. The model sees only the 15 minutes of polls before the alert, with no position, callsign, date or absolute heading, and every value rounded, so it can't learn which dataset an example came from instead of what the aircraft did.

Two triage models were compared, **trained on the same 903 dev examples** and scored on the full test split:

| Test split (3,154 alerts) | Gradient-boosted trees | Qwen2.5-1.5B-Instruct + LoRA |
| --- | --- | --- |
| ROC-AUC, real vs routine | **0.971** | 0.949 |
| Average precision | **0.756** | 0.514 |
| ROC-AUC, same source only | **0.877** | 0.855 |
| Routine alerts kept at 80% recall of real ones | **70** (Barcelona: 54) | 138 (Barcelona: 103) |
| Cause accuracy, known causes | 8% | 11.5% |

The LLM (`aeroscan/triage/train.py`, LoRA r=16, 3 epochs on an RTX 4070 Ti SUPER) is scored by the likelihood it gives each allowed answer, so both models are compared at identical operating points.

**Result: the fine-tuned LLM does not beat a conventional model here.** At the same detection rate it lets through about twice as many false alarms. With a few hundred examples of short numeric sequences, trees on hand-built features are the better tool. The gradient-boosted model trained on all 3,105 dev alerts does better still (ROC-AUC 0.975; 53 routine alerts kept at 80% recall).

The same-source row is the honest one: real alerts against the normal part of the very same 7700 flights, so it can't be inflated by recognising the dataset. Neither model can name the cause: movement alone, with about 80 labelled examples, does not tell an engine problem from a medical one.

Raw numbers: [`eval/results/triage_comparison.json`](eval/results/triage_comparison.json).

## Evaluation method

- **Data.**
  - Emergencies: [squawk7700](https://doi.org/10.5281/zenodo.3937483), 832 flights that squawked 7700 between Jan 2018 and Jan 2020, with problem labels, by X. Olive et al.
  - Barcelona traffic: 12 days of 2025 from the [adsb.lol](https://adsb.lol) open archive, one per month, alternating Wednesdays and Saturdays, cut to the monitored region (`aeroscan/eval/adsblol.py`). Coverage check on 16 July 2025: 1,085 El Prat movements reconstructed against Aena's July average of 1,086 a day, 99.6% of arrivals tracked to the runway, and 97.5% of 60 s polls receiving a fresh report.
  - Additional routine negatives: [arrival trajectories at Dublin, 2019](https://doi.org/10.4121/20411868.v1).
- **Polling simulation.** Each dense trajectory is resampled to what a 60 s poller on a fixed UTC grid would receive: the latest report, if it is no older than one interval, otherwise nothing (`aeroscan/eval/replay.py`).
- **Detection.** A MEDIUM-or-higher trajectory alert between 10 min before and 30 min after the first 7700 report.
- **False alarms.** MEDIUM-or-higher trajectory alerts on ordinary flights. Barcelona flights are split into flights at gaps of more than 20 minutes and must be at least 5 minutes long. Holding is informational and not counted.
- **Split.** Stable 50/50 by hash of flight ID. Every threshold was chosen on dev data only:
  1. Climb gate on turnbacks: before it, all 73 dev false alarms on the 7700 flights were turnbacks, mostly departures reversing while still climbing. The gate cut them to 16 for a loss of 8 of 93 detections.
  2. Departure-phase and speed gates on turnbacks: Barcelona and Palma departures take off eastbound over the sea and turn west, and on short legs reach cruise during the turn. Ignoring reversals by aircraft seen below 5,000 ft earlier in the window, and by aircraft slower than 220 kt (light aircraft), cut Barcelona dev flags from 51 to 32 for a loss of 6 of 106 detections.
  3. Descent threshold: −3,500 ft/min flagged 0.6% of ordinary Barcelona dev flights; −3,750 cut that to 0.2% for a loss of 8 of 100 detections.

## Rules

| Rule | Fires when | Severity |
| --- | --- | --- |
| `squawk_emergency` | 7500 / 7600 / 7700 on 2 consecutive polls (filters accidental selections) | HIGH (7600: MEDIUM) |
| `rapid_descent` | ≤ −3,750 ft/min averaged over 2 consecutive minutes, starting ≥ 15,000 ft | HIGH |
| `turnback` | Track reversed ≥ 135° and held, ≥ 10,000 ft and ≥ 220 kt, not looping, not climbing > 1,000 ft/min, not in the departure phase | MEDIUM |
| `holding` | ≥ 330° of same-direction turn within 12 min | INFO |

Rates come from altitude differences between polls, not the reported instantaneous vertical rate. Implausible jumps (> 20,000 ft/min) are treated as corrupt reports, and the detector never reasons across coverage gaps.

## Quick start

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run pytest                       # unit tests
uv run aeroscan fetch-data          # ~1 GB, checksummed, cached in ./data
uv run aeroscan fetch-data --bcn    # plus the Barcelona days: ~35 GB downloaded, ~1 h, ~200 MB kept
uv run aeroscan eval                # writes eval/results/
```

`aeroscan eval --skip-bcn` runs without the Barcelona data; `--limit 50` is a quick smoke test.

## Roadmap

1. ~~Rules and offline evaluation~~
2. ~~Alert triage: learned real-vs-routine filter behind looser rules (gradient boosting beat a fine-tuned 1.5B LLM)~~
3. Live ingestion from the OpenSky REST API (OAuth2 client credentials, 60 s polls over the Barcelona box = 1 credit per poll) and alert storage
4. REST API and map UI

## Data and attribution

Trajectory data: The OpenSky Network, https://opensky-network.org. M. Schäfer, M. Strohmeier, V. Lenders, I. Martinovic, M. Wilhelm, *Bringing up OpenSky: A large-scale ADS-B sensor network for research*, IPSN 2014.

squawk7700 dataset: X. Olive, A. Tanner, M. Strohmeier, M. Schäfer, M. Feridun, A. Tart, I. Martinovic, V. Lenders, *Reference datasets for in-flight emergency situations*, Zenodo, 2020, doi:10.5281/zenodo.3937483. Dublin arrivals: 4TU.ResearchData, doi:10.4121/20411868.v1 (CC BY-NC-SA 3.0).

Barcelona traffic: © adsb.lol contributors, https://adsb.lol, made available under the [Open Database License 1.0](https://opendatacommons.org/licenses/odbl/1-0/).

The datasets are not redistributed here; `fetch-data` downloads them from their publishers. Published results are aggregated and contain no aircraft identifiers.

## Licence

Code: MIT. Data: see above; the OpenSky data terms restrict use to non-commercial research.
