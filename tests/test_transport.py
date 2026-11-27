import io
import json
import tempfile
import unittest
import urllib.error

from catalog_mirror.transport import Transport, SourceBlocked, SourceMissing, SourceUnavailable


class HTTPResult:
    def __init__(self, body=b"{}", content_type="application/json", status=200):
        self.body, self.status = body, status
        self.headers = {"Content-Type": content_type}
    def read(self, count): return self.body[:count]
    def __enter__(self): return self
    def __exit__(self, *args): pass


class Opener:
    def __init__(self, replies): self.replies, self.calls = list(replies), []
    def open(self, req, timeout):
        self.calls.append(req.full_url)
        result = self.replies.pop(0)
        if isinstance(result, BaseException): raise result
        return result


class TransportTests(unittest.TestCase):
    def transport(self, replies, **kwargs):
        self.opener = Opener(replies)
        self.delays = []
        return Transport(opener=self.opener, sleep=self.delays.append, monotonic=lambda: 1, **kwargs)
    def robots(self, value="User-agent: *\nAllow: /\n"):
        return HTTPResult(value.encode(), "text/plain")
    def test_cache_and_conservative_rate(self):
        t = self.transport([self.robots(), HTTPResult()])
        t.get("https://metal-api.dev/bands/1")
        self.assertTrue(t.get("https://metal-api.dev/bands/1").from_cache)
        self.assertEqual(len(self.opener.calls), 2)
        self.assertEqual(self.delays, [5])
    def test_forbidden_paths_hosts_and_insecure_requests(self):
        for url in ("https://forum.metal-archives.com/", "https://www.metal-archives.com/forum/",
                    "https://www.metal-archives.com/images/1.jpg", "https://www.metal-archives.com/user/1",
                    "http://www.metal-archives.com/bands/1", "https://evil.test/bands/1",
                    "https://www.metal-archives.com/bands/../login"):
            with self.subTest(url=url), self.assertRaises(SourceBlocked):
                Transport.validate_url(url)
    def test_robots_disallowed_stops_catalog(self):
        t = self.transport([self.robots("User-agent: *\nDisallow: /\n")])
        with self.assertRaises(SourceBlocked): t.get("https://metal-api.dev/bands/1")
        self.assertEqual(len(self.opener.calls), 1)
    def test_403_stops_host_without_retry(self):
        error = urllib.error.HTTPError("https://metal-api.dev/bands/1", 403, "Forbidden", {}, None)
        t = self.transport([self.robots(), error])
        with self.assertRaises(SourceBlocked): t.get("https://metal-api.dev/bands/1")
        with self.assertRaises(SourceBlocked): t.get("https://metal-api.dev/bands/2")
        self.assertEqual(len(self.opener.calls), 2)
    def test_429_persists_retry_after(self):
        error = urllib.error.HTTPError("https://metal-api.dev/bands/1", 429, "Rate limited", {"Retry-After": "7200"}, None)
        with tempfile.TemporaryDirectory() as d:
            t = self.transport([self.robots(), error], cache_dir=d, now=lambda: 100)
            with self.assertRaises(SourceBlocked): t.get("https://metal-api.dev/bands/1")
            again = self.transport([], cache_dir=d, now=lambda: 7100)
            with self.assertRaises(SourceBlocked): again.get("https://metal-api.dev/bands/1")
            self.assertEqual(self.opener.calls, [])
    def test_captcha_is_not_empty_catalog(self):
        t = self.transport([self.robots(), HTTPResult(b"<html>verify you are human</html>", "text/html")])
        with self.assertRaises(SourceBlocked): t.get("https://metal-api.dev/bands/1")
    def test_only_404_410_are_missing(self):
        for code in (404, 410, 500):
            with self.subTest(code=code):
                t = self.transport([self.robots(), urllib.error.HTTPError("x", code, "Error", {}, None)], max_retries=0)
                expected = SourceMissing if code in (404, 410) else SourceUnavailable
                with self.assertRaises(expected) as caught: t.get("https://metal-api.dev/bands/1")
                self.assertEqual(isinstance(caught.exception, SourceMissing), code in (404, 410))
    def test_response_type_and_size_are_bounded(self):
        for result in (HTTPResult(b"PNG", "image/png"), HTTPResult(b"x" * 4_000_001)):
            t = self.transport([self.robots(), result])
            with self.assertRaises(SourceUnavailable): t.get("https://metal-api.dev/bands/1")
    def test_no_aggressive_interval(self):
        with self.assertRaises(ValueError): Transport(min_interval=0)


if __name__ == "__main__": unittest.main()
