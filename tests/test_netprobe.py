"""netprobe の出力解析のテスト.  python3 -m unittest discover -s tests"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "image"))
import netprobe  # noqa: E402

PING_OK = """PING 1.1.1.1 (1.1.1.1) 56(84) bytes of data.

--- 1.1.1.1 ping statistics ---
20 packets transmitted, 19 received, 5% packet loss, time 9512ms
rtt min/avg/max/mdev = 4.773/5.428/6.616/0.498 ms
"""

PING_ALL_LOST = """PING 192.0.2.1 (192.0.2.1) 56(84) bytes of data.

--- 192.0.2.1 ping statistics ---
10 packets transmitted, 0 received, 100% packet loss, time 9200ms
"""

PING_DNS_ERROR = "ping: no-such-host.invalid: Name or service not known\n"

OOKLA_OUTPUT = "\n".join([
    "==============================================================================",
    "License acceptance recorded. Continuing.",
    json.dumps({"type": "log", "level": "error", "message": "Error: [101] Network unreachable"}),
    json.dumps({
        "type": "result",
        "ping": {"jitter": 0.2, "latency": 5.4},
        "download": {"bandwidth": 11698471, "bytes": 71360640, "latency": {"iqm": 24.1}},
        "upload": {"bandwidth": 11690438, "bytes": 48001540, "latency": {"iqm": 80.5}},
        "packetLoss": 0.5,
        "isp": "Example ISP",
        "server": {"id": 48463, "name": "Example Server", "location": "Tokyo"},
        "result": {"url": "https://www.speedtest.net/result/c/xxx"},
    }),
])


class TestParsePing(unittest.TestCase):
    def test_ok(self):
        r = netprobe.parse_ping(PING_OK, 20)
        self.assertEqual((r["sent"], r["received"]), (20, 19))
        self.assertAlmostEqual(r["loss"], 0.05)
        self.assertEqual(r["rtt_ms"], {"min": 4.773, "avg": 5.428, "max": 6.616, "mdev": 0.498})
        self.assertTrue(r["parsed"])

    def test_all_lost(self):
        r = netprobe.parse_ping(PING_ALL_LOST, 10)
        self.assertEqual(r["loss"], 1.0)
        self.assertIsNone(r["rtt_ms"])
        self.assertTrue(r["parsed"])

    def test_error_counts_as_full_loss(self):
        r = netprobe.parse_ping(PING_DNS_ERROR, 20)
        self.assertEqual((r["sent"], r["received"], r["loss"]), (20, 0, 1.0))
        self.assertFalse(r["parsed"])


class TestOokla(unittest.TestCase):
    def test_parse_and_summarize(self):
        result, errors = netprobe.parse_ookla_output(OOKLA_OUTPUT)
        self.assertEqual(errors, ["Error: [101] Network unreachable"])
        s = netprobe.summarize_ookla(result)
        self.assertEqual(s["download_bps"], 11698471 * 8)
        self.assertEqual(s["upload_bps"], 11690438 * 8)
        self.assertEqual(s["latency_upload_ms"], 80.5)
        self.assertEqual(s["server_id"], "48463")

    def test_no_result(self):
        result, errors = netprobe.parse_ookla_output('{"type":"log","level":"error","message":"boom"}')
        self.assertIsNone(result)
        self.assertEqual(errors, ["boom"])

    def test_requires_license_acceptance(self):
        with self.assertRaises(RuntimeError):
            netprobe.speedtest_ookla({"enabled": True})


if __name__ == "__main__":
    unittest.main()
