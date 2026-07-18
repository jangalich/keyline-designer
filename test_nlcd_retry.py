"""
test_nlcd_retry.py

Regression test for nlcd_data.py's retry/backoff behavior. Prompted by a
real-world report of frequent timeouts/connection failures against
Planetary Computer — up to ~70% of attempts failing during periods of
load. At that failure rate, the original 2-retries/fixed-2s-pause pattern
(borrowed from soil_data.py's SDA queries, which fail far less often) left
roughly a 1-in-3 chance of giving up even after 3 total attempts
(0.7^3 ≈ 34%).

No network access required — this drives nlcd_data._retry() directly with
a fake operation and mocks time.sleep so the test runs instantly rather
than actually waiting out the backoff schedule.
"""

from unittest.mock import patch

import nlcd_data

# --- Recovers from repeated failures within the retry budget ---
calls = []


def fails_four_times_then_succeeds(timeout):
    calls.append(timeout)
    if len(calls) < 5:
        raise ConnectionError("simulated Planetary Computer failure")
    return "success"


sleep_calls = []
with patch("time.sleep", side_effect=lambda s: sleep_calls.append(s)):
    result = nlcd_data._retry(fails_four_times_then_succeeds)

assert result == "success", f"expected eventual success, got {result!r}"
assert len(calls) == 5, f"expected 5 attempts before success, got {len(calls)}"
print(f"Recovered after {len(calls)} attempts (4 simulated failures + 1 success).")

# Backoff between attempts should be exponential (2s, 4s, 8s, 16s, ...),
# not a fixed pause — gives transient load/connectivity issues room to
# clear instead of hammering the same failure immediately.
assert sleep_calls == [2, 4, 8, 16], f"expected exponential backoff, got {sleep_calls}"
print(f"Backoff sequence is exponential: {sleep_calls}")

# --- Enough retries to survive a high failure rate, without retrying forever ---
assert nlcd_data.DEFAULT_MAX_RETRIES >= 5, (
    f"DEFAULT_MAX_RETRIES={nlcd_data.DEFAULT_MAX_RETRIES} is too low for a "
    f"~70% per-attempt failure rate (2 retries/3 attempts fails end-to-end "
    f"~34% of the time; this needs meaningfully better odds than that)"
)
total_attempts = nlcd_data.DEFAULT_MAX_RETRIES + 1
failure_rate_at_70pct = 0.7 ** total_attempts
assert failure_rate_at_70pct < 0.15, (
    f"{total_attempts} attempts still fails {failure_rate_at_70pct:.1%} of the "
    f"time at a 70% per-attempt failure rate — not enough of an improvement"
)
print(
    f"DEFAULT_MAX_RETRIES={nlcd_data.DEFAULT_MAX_RETRIES} ({total_attempts} attempts) "
    f"brings a 70%-per-attempt failure rate down to {failure_rate_at_70pct:.1%} "
    f"end-to-end failure odds."
)

# --- Exhausting all retries re-raises the real error, and both the
#     per-attempt timeout and the backoff pause stay capped rather than
#     growing unbounded as attempts increase ---
calls_exhausted = []


def always_fails(timeout):
    calls_exhausted.append(timeout)
    raise TimeoutError(f"simulated timeout on attempt {len(calls_exhausted)}")


sleep_calls_exhausted = []
with patch("time.sleep", side_effect=lambda s: sleep_calls_exhausted.append(s)):
    try:
        nlcd_data._retry(always_fails)
        raise AssertionError("_retry should have raised after exhausting all attempts")
    except TimeoutError:
        pass

assert len(calls_exhausted) == nlcd_data.DEFAULT_MAX_RETRIES + 1
assert max(calls_exhausted) <= nlcd_data._MAX_TIMEOUT_SECONDS
assert max(sleep_calls_exhausted) <= nlcd_data._MAX_BACKOFF_SECONDS
print(
    f"Exhausting all {len(calls_exhausted)} attempts re-raises the real error; "
    f"timeout capped at {max(calls_exhausted)}s, backoff capped at "
    f"{max(sleep_calls_exhausted)}s."
)

print("\nAll NLCD retry/backoff checks passed.")
