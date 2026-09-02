"""Regression tests for fetch_direct_to_db's year-by-year chunking.

For ranges longer than 365 days, fetch_direct_to_db in garmin_givemydata.py
walks backward from end_date in ~365-day chunks. The chunk boundaries must
tile [start_date, end_date] exactly — every calendar day fetched exactly
once, none skipped, none re-fetched.

Bug found in code review: because a calendar year can be 365 or 366 days,
`cursor - timedelta(days=365)` does not always land exactly on start_date
even when it "should". The original loop guard (`while cursor > s`) then
drops that leftover single day silently — the *earliest* day of the
requested range never gets fetched, and only when total_days is an exact
multiple of 366 (366, 732, 1098, 1464, ...). A --days 1095 or a 10-year
--full run happens not to land on one of these values, which is why this
went unnoticed; other ranges do land on them.
"""

from datetime import date, timedelta

import pytest

from garmin_givemydata import fetch_direct_to_db


class _RecordingClient:
    """Stand-in for GarminClient.fetch_all: records the (start, end) pair
    each call was asked to cover, and reports a nonzero batch each time so
    the "no data in this chunk, stopping" early-exit never masks a gap."""

    def __init__(self):
        self.calls = []

    def fetch_all(self, target_date, start_date, end_date, on_batch, known_activity_ids, save_raw=False):
        self.calls.append((start_date, end_date))
        on_batch("daily_summary", [{"calendar_date": start_date}])


@pytest.fixture(autouse=True)
def _stub_save_to_db(monkeypatch):
    """Isolate the chunking geometry from save_to_db's real row-shape
    validation: always report 1 row saved so the "no data in this chunk,
    stopping" early-exit never masks a date-coverage gap in these tests."""
    monkeypatch.setattr("garmin_givemydata.save_to_db", lambda conn, endpoint_name, data, cal_date=None: 1)


def _covered_days(calls):
    covered = []
    for start_date, end_date in calls:
        d = date.fromisoformat(start_date)
        end = date.fromisoformat(end_date)
        while d <= end:
            covered.append(d)
            d += timedelta(days=1)
    return covered


@pytest.mark.parametrize(
    "start_date,end_date",
    [
        ("2025-01-01", "2025-12-30"),  # 364 days: single-chunk path (<=365)
        ("2025-01-01", "2025-12-31"),  # 365 days: single-chunk boundary
        ("2020-01-01", "2021-01-01"),  # 366 days: previously dropped start_date
        ("2019-01-01", "2021-01-02"),  # 732 days: previously dropped start_date
        ("2018-01-01", "2021-01-03"),  # 1098 days: previously dropped start_date
        ("2023-09-01", "2026-08-31"),  # 1095 days: the --days 1095 case used in production
        ("2016-08-31", "2026-08-29"),  # 3650 days: the old --full 10-year default
    ],
)
def test_chunks_cover_full_range_with_no_gaps_or_overlaps(temp_db, start_date, end_date, monkeypatch):
    monkeypatch.setattr(
        "garmin_givemydata.db_query",
        lambda conn, sql, *a, **k: [],  # no pre-existing activity_splits rows
    )

    client = _RecordingClient()
    fetch_direct_to_db(client, temp_db, start_date, end_date)

    covered = _covered_days(client.calls)
    expected_start = date.fromisoformat(start_date)
    expected_end = date.fromisoformat(end_date)

    expected = set()
    d = expected_start
    while d <= expected_end:
        expected.add(d)
        d += timedelta(days=1)

    covered_set = set(covered)
    missing = expected - covered_set
    assert not missing, f"chunking dropped days from the requested range: {sorted(missing)[:10]}"

    duplicates = len(covered) - len(covered_set)
    assert duplicates == 0, f"chunking re-fetched {duplicates} day(s) across overlapping chunks"

    extra = covered_set - expected
    assert not extra, f"chunking fetched days outside the requested range: {sorted(extra)[:10]}"
