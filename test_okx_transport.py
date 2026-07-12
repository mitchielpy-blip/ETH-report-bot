"""
Tests for the OKX transport helper (okx_get): it must route requests through
the optional OKX_PROXY and, crucially, turn OKX's geo-block redirect into a
clear, actionable error instead of a confusing downstream JSON failure.

Run with:  python -m unittest test_okx_transport
"""

import unittest
from unittest import mock

import eth_report_bot as bot


class _Resp:
    """Minimal stand-in for a requests.Response."""
    def __init__(self, status_code=200, json_data=None, location=None):
        self.status_code = status_code
        self._json = json_data if json_data is not None else {}
        self.headers = {"Location": location} if location else {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise bot.requests.HTTPError(str(self.status_code))

    def json(self):
        return self._json


class OkxGet(unittest.TestCase):
    def test_returns_json_and_forwards_proxy(self):
        payload = {"code": "0", "data": [[1, 2]]}
        proxies = {"https": "http://proxy:8080", "http": "http://proxy:8080"}
        with mock.patch.object(bot, "OKX_PROXIES", proxies), \
             mock.patch.object(bot.requests, "get", return_value=_Resp(200, payload)) as g:
            out = bot.okx_get("/api/v5/market/candles", {"instId": "ETH-USDT-SWAP"})
        self.assertEqual(out, payload)
        _, kwargs = g.call_args
        self.assertEqual(kwargs["proxies"], proxies)
        # redirects must not be auto-followed, or the geo-block hides itself
        self.assertFalse(kwargs["allow_redirects"])

    def test_no_proxy_calls_okx_directly(self):
        with mock.patch.object(bot, "OKX_PROXIES", None), \
             mock.patch.object(bot.requests, "get", return_value=_Resp(200, {"code": "0"})) as g:
            bot.okx_get("/api/v5/market/ticker")
        _, kwargs = g.call_args
        self.assertIsNone(kwargs["proxies"])

    def test_redirect_raises_clear_geoblock_error(self):
        resp = _Resp(307, location="https://www.okx.com/help/restricted")
        with mock.patch.object(bot.requests, "get", return_value=resp):
            with self.assertRaises(RuntimeError) as ctx:
                bot.okx_get("/api/v5/market/ticker", {"instId": "ETH-USDT-SWAP"})
        msg = str(ctx.exception)
        self.assertIn("307", msg)
        self.assertIn("OKX_PROXY", msg)  # tells the operator exactly what to set


class FetchCandlesSurfacesGeoblock(unittest.TestCase):
    def test_geoblock_message_survives_the_retry_loop(self):
        resp = _Resp(307, location="/restricted")
        with mock.patch.object(bot.time, "sleep", lambda *a, **k: None), \
             mock.patch.object(bot.requests, "get", return_value=resp):
            with self.assertRaises(RuntimeError) as ctx:
                bot.fetch_candles()
        self.assertIn("OKX_PROXY", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
