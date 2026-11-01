"""Machine-learning layer of Household Intelligence.

It reads what the data-engineering layer publishes and builds, step by step:
  1. asof.py       what was KNOWN at a given moment (prevents time leakage)
  2. (next)        the prediction target and the baseline model
"""