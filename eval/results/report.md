# Aero-Scan evaluation (60 s polling)

## dev split

| Metric | Value |
| --- | --- |
| 7700 flights | 435 |
| Trajectory-only recall (all) | 24.4% |
| Trajectory-only recall (diverted) | 56.1% |
| Detected before the 7700 squawk | 26 |
| Median lead time vs squawk | 161.0 s |
| False alarms, normal part of 7700 flights | 16 in 271.0 h (0.059/h) |
| False alarms, Dublin approaches | 95 of 8945 flights (1.06%) |
| Squawk rule recall (squawk visible) | 99.1% |

| Category | Flights | Detected | Recall |
| --- | --- | --- | --- |
| unlabelled | 226 | 10 | 4% |
| unclear | 100 | 43 | 43% |
| medical | 34 | 14 | 41% |
| engine | 15 | 4 | 27% |
| cabin_pressure | 14 | 10 | 71% |
| technical | 10 | 3 | 30% |
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
| heating | 1 | 1 | 100% |
| fuel_leak | 1 | 0 | 0% |
| flaps | 1 | 0 | 0% |

## test split

| Metric | Value |
| --- | --- |
| 7700 flights | 397 |
| Trajectory-only recall (all) | 28.2% |
| Trajectory-only recall (diverted) | 59.9% |
| Detected before the 7700 squawk | 25 |
| Median lead time vs squawk | 160.0 s |
| False alarms, normal part of 7700 flights | 16 in 288.7 h (0.0554/h) |
| False alarms, Dublin approaches | 87 of 8756 flights (0.99%) |
| Squawk rule recall (squawk visible) | 100.0% |

| Category | Flights | Detected | Recall |
| --- | --- | --- | --- |
| unlabelled | 186 | 10 | 5% |
| unclear | 104 | 39 | 38% |
| medical | 40 | 24 | 60% |
| technical | 11 | 6 | 55% |
| smoke_burn_smell_flames | 11 | 8 | 73% |
| engine | 10 | 7 | 70% |
| cabin_pressure | 9 | 8 | 89% |
| fuel_leak | 7 | 3 | 43% |
| hydraulics | 4 | 1 | 25% |
| misc | 2 | 1 | 50% |
| landing_gear | 2 | 1 | 50% |
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
