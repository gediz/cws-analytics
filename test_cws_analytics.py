"""Tests for cws_analytics decode/encode. Uses synthetic batchexecute responses, no real data.

Run with:  python -m unittest
"""
import json
import os
import tempfile
import unittest

import cws_analytics as c


def _scalar_payload(day, value, label):
    """GetItemStats scalar payload: the series sits in data[1]."""
    return [["ext", [2026, 6, 22]], [[day, [[4, [[3, value, label]]]]]], None]


def _dim_payload(day, pairs):
    """GetItemStats dimension payload: the series sits in data[2]; pairs = [(label, count), ...]."""
    rows = [["COUNTRY", count, label] for label, count in pairs]
    return [["ext", [2026, 6, 22]], None, [[day, rows]]]


def _wrb(rpc, payload, idx):
    return ["wrb.fr", rpc, c._compact(payload), None, None, None, idx]


def _chunked(rows):
    """The rt=c, length-prefixed framing."""
    body = ")]}'\n"
    for row in rows:
        chunk = c._compact([row])
        body += f"{len(chunk)}\n{chunk}\n"
    return body


def _single_array(rows):
    """The rt=default, single-JSON-array framing."""
    return ")]}'\n" + c._compact(rows)


class TestEncode(unittest.TestCase):
    def test_build_freq_uses_odd_indices(self):
        arr = json.loads(c.build_freq([("A", "[]"), ("B", "[]"), ("C", "[]")]))[0]
        self.assertEqual([sub[3] for sub in arr], ["3", "5", "7"])

    def test_stats_inner_shape(self):
        self.assertEqual(json.loads(c.stats_inner("X", 1, 5, 100, 200)),
                         [[[None, [100, 200]], "X", 1, 5]])
        self.assertEqual(json.loads(c.stats_inner("X", 4, None, 0, 200)),
                         [[[None, [0, 200]], "X", 4]])


class TestDecode(unittest.TestCase):
    def setUp(self):
        self.rows = [
            _wrb("WlSRsc", _scalar_payload("2026-06-22", 15344, "WEEKLY_USERS"), "3"),
            _wrb("WlSRsc", _dim_payload("2026-06-22", [("Germany", 100), ("United States", 50)]), "5"),
        ]
        self.idx = {"3": (4, None), "5": (1, 3)}  # weekly_users, installs_by_country

    def test_chunked_framing(self):
        series = c.decode_stats(_chunked(self.rows), self.idx)
        self.assertEqual(series["weekly_users"]["2026-06-22"], 15344)
        self.assertEqual(series["installs_by_country"]["2026-06-22"]["Germany"], 100)

    def test_single_array_framing_decodes_identically(self):
        series = c.decode_stats(_single_array(self.rows), self.idx)
        self.assertEqual(series["weekly_users"]["2026-06-22"], 15344)
        self.assertEqual(series["installs_by_country"]["2026-06-22"]["United States"], 50)

    def test_same_dimension_under_two_groups_does_not_collide(self):
        rows = [
            _wrb("WlSRsc", _dim_payload("2026-06-22", [("Germany", 10000)]), "3"),
            _wrb("WlSRsc", _dim_payload("2026-06-22", [("Canada", 20)]), "5"),
        ]
        series = c.decode_stats(_chunked(rows), {"3": (4, 3), "5": (2, 3)})
        self.assertEqual(series["weekly_users_by_country"]["2026-06-22"], {"Germany": 10000})
        self.assertEqual(series["uninstalls_by_country"]["2026-06-22"], {"Canada": 20})


class TestRatings(unittest.TestCase):
    def test_parse_ratings(self):
        payload = [["ext"], [["2026-06-22", None, None, None, None, None, [0, 0, 0, 1, 13]]]]
        stars = c.parse_ratings(_chunked([_wrb("vjWKuf", payload, "3")]))
        self.assertEqual(stars["2026-06-22"], [0, 0, 0, 1, 13])

    def test_parse_ratings_pads_short_buckets(self):
        payload = [["ext"], [["2026-06-22", None, None, None, None, None, [0, 2]]]]
        stars = c.parse_ratings(_chunked([_wrb("vjWKuf", payload, "3")]))
        self.assertEqual(stars["2026-06-22"], [0, 2, 0, 0, 0])

    def test_parse_ratings_tolerates_short_payload(self):
        self.assertEqual(c.parse_ratings(_chunked([_wrb("vjWKuf", [["ext"]], "3")])), {})


class TestRobustness(unittest.TestCase):
    def test_first_payload_handles_already_decoded(self):
        # a framing variant could hand back row[2] already decoded; must not raise
        row = ["wrb.fr", "Mt2BP", {"x": 1}, None, None, None, "3"]
        self.assertEqual(c.first_payload(_chunked([row]), "Mt2BP"), {"x": 1})

    def test_loads_tolerates_bad_json(self):
        self.assertIsNone(c._loads("{not json"))
        self.assertEqual(c._loads([1, 2]), [1, 2])


class TestSessionAndGuards(unittest.TestCase):
    def _client(self, ext="a" * 32, account="00000000-0000-0000-0000-000000000000"):
        fd, path = tempfile.mkstemp()
        with os.fdopen(fd, "w") as fh:
            fh.write(f"CWS_COOKIE=x\nCWS_EXTENSION_ID={ext}\nCWS_ACCOUNT_ID={account}\n")
        self.addCleanup(os.unlink, path)
        return c.CwsClient(c.Session(path))

    def test_session_rejects_bad_extension_id(self):
        with self.assertRaises(c.CwsError):
            self._client(ext="not-a-valid-id")

    def test_session_rejects_bad_account_id(self):
        with self.assertRaises(c.CwsError):
            self._client(account="nope")

    def test_stats_rejects_future_start(self):
        with self.assertRaises(c.CwsError):
            self._client().stats(day_start=c.today_epoch() + 10)

    def test_ratings_rejects_future_start(self):
        with self.assertRaises(c.CwsError):
            self._client().ratings(day_start=c.today_epoch() + 10)


if __name__ == "__main__":
    unittest.main()
