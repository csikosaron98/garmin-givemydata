"""incremental_sync must parse trackpoints only for activities it just fetched.

`known_activity_ids` is built to tell ``fetch_all`` which activities to SKIP,
because their details are already stored. It was also handed to
``_parse_trackpoints_for_activities``, which inverts its meaning: instead of
"skip these", it became "re-parse all of these". Every sync therefore re-read
the whole FIT archive and re-upserted the entire trackpoint history — on one
real database, ~915 000 rows rewritten per run to add the two or three that
were actually new, taking a routine sync to about six minutes.

Nothing failed, so nothing surfaced it. The data stayed correct; only the clock
showed the cost.
"""

import os
import unittest
from unittest.mock import MagicMock, patch

PREEXISTING = [(111,), (222,)]


def _run_sync(fetch_side_effect):
    """Drive incremental_sync with a stubbed client, capturing the trackpoint call."""
    conn = MagicMock()
    conn.execute.return_value.fetchall.return_value = PREEXISTING

    client_cls = MagicMock()
    client = client_cls.return_value
    client.login.return_value = True
    client.fetch_all.side_effect = fetch_side_effect

    with (
        patch("garmin_client.GarminClient", client_cls),
        patch("garmin_mcp.sync.get_connection", return_value=conn),
        patch("garmin_mcp.sync.init_db"),
        patch("garmin_mcp.sync.save_to_db", return_value=1),
        patch("garmin_mcp.sync._parse_trackpoints_for_activities", return_value=0) as parse,
        patch.dict(os.environ, {"GARMIN_EMAIL": "x", "GARMIN_PASSWORD": "y"}),
    ):
        from garmin_mcp.sync import incremental_sync

        result = incremental_sync()

    return result, parse, client


class TestTrackpointScope(unittest.TestCase):
    def setUp(self):
        self._cwd = os.getcwd()

    def tearDown(self):
        try:
            os.chdir(self._cwd)
        except OSError:
            pass

    def test_only_newly_fetched_activities_are_parsed(self):
        """One new activity arrives; only it gets its trackpoints parsed."""

        def fetch(**kwargs):
            kwargs["on_batch"]("activity_splits", [{"x": 1}], cal_date="999")

        _, parse, _ = _run_sync(fetch)

        parse.assert_called_once()
        parsed_ids = parse.call_args[0][1]
        self.assertEqual(
            set(parsed_ids),
            {999},
            "only the activity fetched in this run should have its trackpoints parsed",
        )
        for old_id in (111, 222):
            self.assertNotIn(
                old_id,
                parsed_ids,
                f"activity {old_id} was already stored — re-parsing it is the six-minute bug",
            )

    def test_nothing_new_parses_nothing(self):
        """The common case: the timer already synced, no new activity arrived."""

        def fetch(**kwargs):
            kwargs["on_batch"]("daily_summary", [{"x": 1}], cal_date="2026-09-02")

        _, parse, _ = _run_sync(fetch)

        parse.assert_called_once()
        self.assertEqual(
            set(parse.call_args[0][1]),
            set(),
            "with no new activities nothing should be re-parsed",
        )

    def test_several_new_activities_are_all_parsed(self):
        def fetch(**kwargs):
            for aid in ("901", "902", "903"):
                kwargs["on_batch"]("activity_splits", [{"x": 1}], cal_date=aid)

        _, parse, _ = _run_sync(fetch)

        self.assertEqual(set(parse.call_args[0][1]), {901, 902, 903})

    def test_unparseable_activity_id_is_ignored(self):
        """A non-numeric cal_date must not crash the sync or poison the set."""

        def fetch(**kwargs):
            kwargs["on_batch"]("activity_splits", [{"x": 1}], cal_date="not-a-number")
            kwargs["on_batch"]("activity_splits", [{"x": 1}], cal_date="777")

        result, parse, _ = _run_sync(fetch)

        self.assertEqual(set(parse.call_args[0][1]), {777})
        self.assertEqual(result["status"], "ok")

    def test_fetch_all_still_receives_the_full_skip_set(self):
        """The skip optimisation must survive: fetch_all still sees the history.

        The fix narrows what gets re-parsed, not what gets skipped. If this
        regresses, the sync starts re-downloading activity details it already
        has — the opposite waste.
        """

        def fetch(**kwargs):
            kwargs["on_batch"]("activity_splits", [{"x": 1}], cal_date="999")

        _, _, client = _run_sync(fetch)

        passed = client.fetch_all.call_args.kwargs["known_activity_ids"]
        self.assertIn(111, passed)
        self.assertIn(222, passed)


class TestParseTrackpointsForActivities(unittest.TestCase):
    def test_empty_set_does_no_work(self):
        from garmin_mcp.sync import _parse_trackpoints_for_activities

        with patch("garmin_mcp.parse_activity_files.parse_trackpoints_from_directory") as p:
            self.assertEqual(_parse_trackpoints_for_activities(MagicMock(), set()), 0)
            p.assert_not_called()


if __name__ == "__main__":
    unittest.main()
