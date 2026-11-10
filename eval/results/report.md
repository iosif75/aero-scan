# Aero-Scan evaluation (60 s polling)

## dev split

| Metric | Value |
| --- | --- |
| 7700 flights | 435 |
| Trajectory-only recall (all) | 21.1% |
| Trajectory-only recall (diverted) | 50.7% |
| Detected before the 7700 squawk | 19 |
| Median lead time vs squawk | 195.0 s |
| False alarms, normal part of 7700 flights | 7 in 271.0 h (0.0258/h) |
| False alarms, Dublin approaches | 78 of 8945 flights (0.87%) |
| Squawk rule recall (squawk visible) | 99.1% |

| Category | Flights | Detected | Recall |
| --- | --- | --- | --- |
| unlabelled | 226 | 5 | 2% |
| unclear | 100 | 39 | 39% |
| medical | 34 | 13 | 38% |
| engine | 15 | 3 | 20% |
| cabin_pressure | 14 | 9 | 64% |
| technical | 10 | 2 | 20% |
| smoke_burn_smell_flames | 10 | 7 | 70% |
| misc | 4 | 1 | 25% |
| landing_gear | 4 | 3 | 75% |
| cracked_windshield | 3 | 2 | 67% |
| bird | 3 | 1 | 33% |
| instrument | 2 | 2 | 100% |
| hydraulics | 2 | 0 | 0% |
| weather_damage | 1 | 1 | 100% |
| brakes | 1 | 1 | 100% |
| air_condition | 1 | 1 | 100% |
| hot_air_leak | 1 | 1 | 100% |
| bomb_threat | 1 | 1 | 100% |
| heating | 1 | 0 | 0% |
| fuel_leak | 1 | 0 | 0% |
| flaps | 1 | 0 | 0% |

## test split

| Metric | Value |
| --- | --- |
| 7700 flights | 397 |
| Trajectory-only recall (all) | 23.7% |
| Trajectory-only recall (diverted) | 52.4% |
| Detected before the 7700 squawk | 17 |
| Median lead time vs squawk | 170.0 s |
| False alarms, normal part of 7700 flights | 10 in 288.7 h (0.0346/h) |
| False alarms, Dublin approaches | 73 of 8756 flights (0.83%) |
| Squawk rule recall (squawk visible) | 100.0% |

| Category | Flights | Detected | Recall |
| --- | --- | --- | --- |
| unlabelled | 186 | 7 | 4% |
| unclear | 104 | 31 | 30% |
| medical | 40 | 22 | 55% |
| technical | 11 | 6 | 55% |
| smoke_burn_smell_flames | 11 | 8 | 73% |
| engine | 10 | 5 | 50% |
| cabin_pressure | 9 | 7 | 78% |
| fuel_leak | 7 | 3 | 43% |
| hydraulics | 4 | 0 | 0% |
| misc | 2 | 1 | 50% |
| landing_gear | 2 | 0 | 0% |
| cracked_windshield | 2 | 1 | 50% |
| maintenance | 1 | 0 | 0% |
| door | 1 | 0 | 0% |
| bomb_threat | 1 | 1 | 100% |
| slats | 1 | 0 | 0% |
| instrument | 1 | 1 | 100% |
| weather_damage | 1 | 0 | 0% |
| bird | 1 | 0 | 0% |
| tyre | 1 | 1 | 100% |
| operational_issue | 1 | 0 | 0% |

## Barcelona traffic (adsb.lol, 12 days of 2025)

| Metric | dev | test |
| --- | --- | --- |
| Flights | 18,696 | 18,696 |
| Flight-hours in region | 9,528 | 9,675 |
| Descents from cruise | 7,462 | 7,569 |
| Flights with a false alarm | 87 | 110 |
| False-alarm rate per flight | 0.47% | 0.59% |
| False alarms per flight-hour | 0.0120 | 0.0129 |
| `rapid_descent` alerts | 47 | 58 |
| `turnback` alerts | 67 | 67 |

Emergency squawks seen: 4 flights, 3 held on consecutive polls (excluded from false alarms), 0 of those also flagged by trajectory rules.
