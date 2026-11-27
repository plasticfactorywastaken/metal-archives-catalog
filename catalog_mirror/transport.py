"""Small, conservative HTTP transport. No authentication, bypasses or image fetches."""
from __future__ import annotations

import email.utils
import hashlib
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from dataclasses import dataclass
from pathlib import Path


class SourceUnavailable(RuntimeError):
    """Unavailable/invalid responses are never evidence that a record was removed."""


class SourceBlocked(SourceUnavailable):
    pass


class SourceMissing(SourceUnavailable):
    def __init__(self, url, status=404):
        self.url, self.status = url, status
        super().__init__(f"Confirmed HTTP {status}: {url}")


@dataclass
class Response:
    url: str
    status: int
    body: bytes
    content_type: str = ""
    from_cache: bool = False

    @property
    def text(self):
        return self.body.decode("utf-8-sig", "replace")

    def json(self):
        try:
            return json.loads(self.text)
        except (ValueError, TypeError) as exc:
            raise SourceUnavailable(f"Invalid JSON from {self.url}") from exc


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, validate):
        self.validate = validate

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Redirecting to another approved host is still refused: source identity matters.
        if urllib.parse.urlsplit(req.full_url).netloc != urllib.parse.urlsplit(newurl).netloc:
            raise SourceBlocked("Cross-host redirect refused")
        self.validate(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class Transport:
    USER_AGENT = "MetalCatalogMirror/1.0 (deterministic metadata sync; no AI parsing)"

    def __init__(self, cache_dir=None, min_interval=5, user_agent=USER_AGENT,
                 timeout=30, max_retries=2, cache_ttl=3600, opener=None,
                 sleep=time.sleep, monotonic=time.monotonic, now=time.time):
        if float(min_interval) < 5:
            raise ValueError("Minimum request interval is five seconds")
        self.min_interval = float(min_interval)
        self.user_agent, self.timeout = user_agent, timeout
        self.max_retries, self.cache_ttl = max_retries, cache_ttl
        self.sleep, self.monotonic, self.now = sleep, monotonic, now
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self._last, self._robots, self._cache, self._blocked = {}, {}, {}, {}
        self._opener = opener or urllib.request.build_opener(_SafeRedirect(self._validate_redirect))
        self.requests = 0

    def _validate_redirect(self, url):
        self.validate_url(url)
        self._check_robots(url)
        self._rate_limit(urllib.parse.urlsplit(url).netloc)

    @staticmethod
    def validate_url(url):
        p = urllib.parse.urlsplit(url)
        if p.scheme != "https" or p.username or p.password or p.port not in (None, 443):
            raise SourceBlocked("Only public HTTPS source URLs are allowed")
        path = urllib.parse.unquote(p.path).lower()
        if ".." in path or "//" in path or any(x in path for x in ("forum", "login", "user/", "users/", "images/", "artwork", "lyrics")):
            raise SourceBlocked("URL outside public catalog scope")
        if p.hostname == "www.metal-archives.com":
            allowed = ("/robots.txt", "/bands/", "/albums/", "/artists/", "/labels/",
                       "/browse/ajax-letter/", "/band/discography/", "/release/ajax-upcoming/")
            if path.startswith(("/bands/", "/albums/", "/artists/", "/labels/")) and not re.search(r"/\d+/?$", path):
                raise SourceBlocked("Public detail URLs must end in a numeric stable ID")
        elif p.hostname == "metal-api.dev":
            allowed = ("/robots.txt", "/swagger/v1/swagger.json", "/bands/", "/albums/", "/search/bands/", "/search/albums/")
        else:
            raise SourceBlocked("Host outside source allowlist")
        if not any(path == a or (a.endswith("/") and path.startswith(a)) for a in allowed):
            raise SourceBlocked("Endpoint outside source allowlist")

    def _rate_limit(self, host):
        delay = self.min_interval - (self.monotonic() - self._last.get(host, -1e10))
        if delay > 0:
            self.sleep(delay)
        self._last[host] = self.monotonic()

    def _request(self, url):
        host = urllib.parse.urlsplit(url).netloc
        if host in self._blocked:
            raise SourceBlocked(self._blocked[host])
        for attempt in range(self.max_retries + 1):
            self._rate_limit(host)
            req = urllib.request.Request(url, headers={"User-Agent": self.user_agent,
                    "Accept": "application/json,text/html,text/plain", "Accept-Encoding": "identity"})
            self.requests += 1
            try:
                with self._opener.open(req, timeout=self.timeout) as result:
                    content_type = result.headers.get("Content-Type", "")
                    if not any(t in content_type.lower() for t in ("json", "html", "text/plain")):
                        raise SourceUnavailable("Unexpected non-text source response")
                    body = result.read(4_000_001)
                    if len(body) > 4_000_000:
                        raise SourceUnavailable("Response exceeded the four MB safety limit")
                    lower = body[:100000].lower()
                    if any(x in lower for x in (b"cf-chl-", b"challenge-platform", b"g-recaptcha", b"hcaptcha", b"verify you are human")):
                        self._blocked[host] = "CAPTCHA or challenge detected; host stopped for this run"
                        raise SourceBlocked(self._blocked[host])
                    return Response(url, result.status, body, content_type)
            except urllib.error.HTTPError as exc:
                if exc.code in (404, 410):
                    raise SourceMissing(url, exc.code) from exc
                if exc.code in (401, 403, 429):
                    retry_after = exc.headers.get("Retry-After", "") if exc.headers else ""
                    self._blocked[host] = f"HTTP {exc.code}; host stopped for this run" + (f"; Retry-After={retry_after}" if retry_after else "")
                    self._save_cooldown(host, retry_after)
                    raise SourceBlocked(self._blocked[host]) from exc
                if exc.code < 500 or attempt == self.max_retries:
                    raise SourceUnavailable(f"HTTP {exc.code}: {url}") from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                if attempt == self.max_retries:
                    raise SourceUnavailable(f"{type(exc).__name__} requesting {url}") from exc
            self.sleep(min(30, 5 * 2 ** attempt))
        raise SourceUnavailable(f"Request failed: {url}")

    def _save_cooldown(self, host, retry_after):
        # A 403 is not bypassed in this run; future scheduled runs may check availability.
        delay = 3600
        if retry_after:
            try:
                delay = max(delay, float(retry_after))
            except ValueError:
                try:
                    delay = max(delay, email.utils.parsedate_to_datetime(retry_after).timestamp() - self.now())
                except (ValueError, TypeError, OverflowError):
                    pass
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            (self.cache_dir / (hashlib.sha256(host.encode()).hexdigest() + ".cooldown.json")).write_text(
                json.dumps({"until": self.now() + delay}), encoding="utf-8")

    def _check_cooldown(self, host):
        if self.cache_dir:
            path = self.cache_dir / (hashlib.sha256(host.encode()).hexdigest() + ".cooldown.json")
            if path.exists():
                try:
                    if json.loads(path.read_text(encoding="utf-8"))["until"] > self.now():
                        raise SourceBlocked("Source is in persisted Retry-After cooldown")
                except (ValueError, KeyError):
                    pass

    def _check_robots(self, url):
        p = urllib.parse.urlsplit(url)
        if p.path == "/robots.txt":
            return
        if p.netloc not in self._robots:
            robots_url = f"https://{p.netloc}/robots.txt"
            parser = urllib.robotparser.RobotFileParser(robots_url)
            try:
                robots = self._request(robots_url)
                if "html" in robots.content_type.lower():
                    raise SourceUnavailable("robots.txt returned HTML; refusing catalog requests")
                parser.parse(robots.text.splitlines())
            except SourceMissing:
                parser.parse(["User-agent: *", "Allow: /"])
            self._robots[p.netloc] = parser
        parser = self._robots[p.netloc]
        if not parser.can_fetch(self.user_agent, url):
            raise SourceBlocked("robots.txt disallows this source URL")
        delay = parser.crawl_delay(self.user_agent)
        if delay:
            self.min_interval = max(self.min_interval, float(delay))

    def get(self, url, *, cache=True):
        self.validate_url(url)
        host = urllib.parse.urlsplit(url).netloc
        self._check_cooldown(host)
        if host in self._blocked:
            raise SourceBlocked(self._blocked[host])
        self._check_robots(url)
        saved = self._cache.get(url)
        if cache and saved and self.now() - saved[0] < self.cache_ttl:
            old = saved[1]
            return Response(old.url, old.status, old.body, old.content_type, True)
        response = self._request(url)
        if cache:
            self._cache[url] = (self.now(), response)
            # Bounded in-memory response cache: source HTML never goes to disk.
            if len(self._cache) > 32:
                del self._cache[next(iter(self._cache))]
        return response
