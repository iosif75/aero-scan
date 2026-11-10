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

## Alert triage (milestone 2, in progress)

The rules above are tuned to keep false alarms down, which costs detections. The alternative being tested: run **looser** rules and put a learned triage step behind them that sorts each alert into *real* or *routine*, plus a likely cause.

Training data comes from every alert of the loose rules (`aeroscan/triage/dataset.py`): 153 real and 2,952 routine alerts in dev, 159 and 2,995 in test. The model sees only the 15 minutes of polls before the alert, with no position, callsign, date or absolute heading, and every value rounded, so it can't learn which dataset an example came from instead of what the aircraft did.

The bar any model has to beat is a gradient-boosted tree baseline on the same inputs (`aeroscan/triage/baseline.py`, test split):

| Metric | Baseline |
| --- | --- |
| ROC-AUC, real vs routine | 0.975 |
| ROC-AUC, same-source only (real vs the normal part of the same 7700 flights) | 0.889 |
| Routine alerts kept while keeping 80% of real ones | 53 of 2,995 (Barcelona: 40) |
| Cause accuracy, known causes | 8% (always answering "unknown" scores 45%) |

The same-source score is the honest one: both sides come from the same dataset, so it can't be inflated by recognising the source. The cause result is a negative finding: movement alone, with about 80 labelled examples, does not tell an engine problem from a medical one.

Next: a LoRA fine-tune of a small instruction model on the same examples (`aeroscan/triage/train.py`), scored on the same metrics.

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
2. LLM triage and explanation: a local model (via Ollama) classifies each alert's likely cause, scored against the dataset's problem labels and against the Barcelona false alarms
3. Live ingestion from the OpenSky REST API (OAuth2 client credentials, 60 s polls over the Barcelona box = 1 credit per poll) and alert storage
4. REST API and map UI

## Data and attribution

Trajectory data: The OpenSky Network, https://opensky-network.org. M. Schäfer, M. Strohmeier, V. Lenders, I. Martinovic, M. Wilhelm, *Bringing up OpenSky: A large-scale ADS-B sensor network for research*, IPSN 2014.

squawk7700 dataset: X. Olive, A. Tanner, M. Strohmeier, M. Schäfer, M. Feridun, A. Tart, I. Martinovic, V. Lenders, *Reference datasets for in-flight emergency situations*, Zenodo, 2020, doi:10.5281/zenodo.3937483. Dublin arrivals: 4TU.ResearchData, doi:10.4121/20411868.v1 (CC BY-NC-SA 3.0).

Barcelona traffic: © adsb.lol contributors, https://adsb.lol, made available under the [Open Database License 1.0](https://opendatacommons.org/licenses/odbl/1-0/).

The datasets are not redistributed here; `fetch-data` downloads them from their publishers. Published results are aggregated and contain no aircraft identifiers.

## Licence

Code: MIT. Data: see above; the OpenSky data terms restrict use to non-commercial research.
