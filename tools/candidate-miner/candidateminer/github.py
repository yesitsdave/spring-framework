"""Minimal GitHub REST client: cached, deterministic, stdlib only.

Design notes worth keeping in mind:

* **One transport.** If `gh` is authenticated we borrow its token via
  ``gh auth token`` and then use ``urllib`` for everything. Shelling out per
  request would be slower and would make caching harder for no benefit.
* **Everything is cached on disk.** Re-running a harvest costs nothing and
  returns identical bytes, which is what makes an inherently network-bound stage
  reproducible enough to sit in this tool.
* **Rate limits fail loudly.** Unauthenticated callers get 60 requests/hour,
  which is not enough for any real harvest. Silently returning a truncated corpus
  would be far worse than stopping.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

API_ROOT = "https://api.github.com"
_USER_AGENT = "candidate-miner"
_TIMEOUT = 30
_RE_NEXT = re.compile(r'<([^>]+)>;\s*rel="next"')
_REDIRECT_CODES = (301, 302, 303, 307, 308)
_PAGE_SUFFIX = ".page.json"


class GitHubError(RuntimeError):
    pass


class GitHubGone(GitHubError):
    """The resource has aged out (404/410) -- expected for old Actions logs."""


class RateLimited(GitHubError):
    def __init__(self, reset_epoch: int | None, authenticated: bool) -> None:
        when = (
            time.strftime("%H:%M:%S", time.localtime(reset_epoch))
            if reset_epoch
            else "unknown"
        )
        hint = (
            "Authenticate to raise the limit from 60 to 5000 requests/hour: "
            "run `gh auth login`, or set GITHUB_TOKEN."
            if not authenticated
            else "Wait for the window to reset, or reduce --limit."
        )
        super().__init__(f"GitHub rate limit exhausted (resets at {when}). {hint}")
        self.reset_epoch = reset_epoch


# `gh auth token` only exists from gh 2.17 or so. Older builds are still very much
# in the wild -- Debian/Ubuntu shipped 2.4 for years -- and there the token is
# reachable through `gh config get`. Try newest first and fall back.
_TOKEN_COMMANDS = (
    ("gh", "auth", "token"),
    ("gh", "config", "get", "-h", "github.com", "oauth_token"),
)


def discover_token() -> str | None:
    """Token from the environment, else from an authenticated `gh`."""
    for name in ("GITHUB_TOKEN", "GH_TOKEN"):
        value = os.environ.get(name)
        if value:
            return value.strip()

    for command in _TOKEN_COMMANDS:
        try:
            proc = subprocess.run(
                command, capture_output=True, text=True, timeout=15
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if proc.returncode != 0:
            # A failing subcommand prints its usage text to stdout, so the return
            # code is the only trustworthy signal here.
            continue
        token = proc.stdout.strip()
        if token and "\n" not in token and len(token) >= 20:
            return token
    return None


@dataclass
class GitHubClient:
    token: str | None = None
    cache_dir: Path | None = None
    requests_made: int = field(default=0, init=False)
    cache_hits: int = field(default=0, init=False)
    # Paths whose pagination hit `max_pages` and stopped early. Callers must
    # surface these: a silently truncated listing reads as "everything", which
    # is how a harvest can quietly under-report.
    truncated: list[str] = field(default_factory=list, init=False)

    @property
    def authenticated(self) -> bool:
        return bool(self.token)

    # -- public API --------------------------------------------------------

    def get(self, path: str) -> Any:
        url = path if path.startswith("http") else f"{API_ROOT}{path}"
        cached = self._cache_read(url)
        if cached is not None:
            self.cache_hits += 1
            return cached
        payload, _ = self._request(url)
        self._cache_write(url, payload)
        return payload

    def get_text(self, path: str) -> str | None:
        """Fetch a plain-text resource (e.g. a job log); None if it is gone.

        Job-log requests answer with a redirect to signed blob storage, and the
        blob host rejects requests that still carry the GitHub ``Authorization``
        header. So redirects are followed *manually*, with credentials dropped
        at the boundary. Expired logs (404/410 -- Actions retains them ~90 days)
        return None rather than raising: an aged-out log is an expected state
        the caller must record, not an error.
        """
        url = path if path.startswith("http") else f"{API_ROOT}{path}"
        cached = self._cache_read_text(url)
        if cached is not None:
            self.cache_hits += 1
            return cached if cached != _TEXT_GONE else None
        try:
            body = self._request_text(url)
        except GitHubGone:
            self._cache_write_text(url, _TEXT_GONE)
            return None
        self._cache_write_text(url, body)
        return body

    def paginate(self, path: str, *, max_pages: int = 100) -> Iterator[Any]:
        url = path if path.startswith("http") else f"{API_ROOT}{path}"
        first = url
        for _ in range(max_pages):
            # Cached under a distinct suffix: a paginated page stores an
            # envelope ({payload, next}), a plain get() stores the bare payload.
            # Sharing one namespace handed get() callers the envelope whenever
            # both methods ever touched the same URL.
            cached = self._cache_read(url, suffix=_PAGE_SUFFIX)
            if cached is not None:
                self.cache_hits += 1
                payload, next_url = cached["payload"], cached["next"]
            else:
                payload, next_url = self._request(url)
                self._cache_write(
                    url, {"payload": payload, "next": next_url}, suffix=_PAGE_SUFFIX
                )
            if isinstance(payload, list):
                yield from payload
            else:
                yield payload
            if not next_url:
                return
            url = next_url
        self.truncated.append(first)

    # -- internals ---------------------------------------------------------

    def _request_text(self, url: str, *, hops: int = 3) -> str:
        headers = {"User-Agent": _USER_AGENT, "X-GitHub-Api-Version": "2022-11-28"}
        authorized = url.startswith(API_ROOT)
        if self.token and authorized:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(url, headers=headers)
        self.requests_made += 1
        opener = urllib.request.build_opener(_NoRedirect)
        try:
            with opener.open(request, timeout=_TIMEOUT) as response:
                return response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            if exc.code in _REDIRECT_CODES:
                location = exc.headers.get("Location")
                if location and hops > 0:
                    return self._request_text(location, hops=hops - 1)
            if exc.code in (404, 410):
                raise GitHubGone(f"{exc.code} for {url}") from exc
            raise GitHubError(f"{exc.code} {exc.reason} for {url}") from exc
        except urllib.error.URLError as exc:
            raise GitHubError(f"network error for {url}: {exc.reason}") from exc

    def _request(self, url: str, *, hops: int = 3) -> tuple[Any, str | None]:
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": _USER_AGENT,
            "X-GitHub-Api-Version": "2022-11-28",
        }
        # Same credential boundary as `_request_text`: redirects are followed
        # manually so the Bearer token is re-decided per hop and never re-sent
        # to whatever off-origin host a Location header names. The default
        # urlopen redirect handler forwards the original headers wholesale.
        if self.token and url.startswith(API_ROOT):
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(url, headers=headers)
        self.requests_made += 1
        opener = urllib.request.build_opener(_NoRedirect)
        try:
            with opener.open(request, timeout=_TIMEOUT) as response:
                body = json.load(response)
                link = response.headers.get("Link", "")
        except urllib.error.HTTPError as exc:
            if exc.code in _REDIRECT_CODES:
                location = exc.headers.get("Location")
                if location and hops > 0:
                    return self._request(location, hops=hops - 1)
            if exc.code in (403, 429):
                remaining = exc.headers.get("X-RateLimit-Remaining")
                if remaining == "0":
                    reset = exc.headers.get("X-RateLimit-Reset")
                    raise RateLimited(
                        int(reset) if reset and reset.isdigit() else None,
                        self.authenticated,
                    ) from exc
            raise GitHubError(f"{exc.code} {exc.reason} for {url}") from exc
        except urllib.error.URLError as exc:
            raise GitHubError(f"network error for {url}: {exc.reason}") from exc

        match = _RE_NEXT.search(link)
        return body, match.group(1) if match else None

    def _cache_path(self, url: str, *, suffix: str = ".json") -> Path | None:
        if self.cache_dir is None:
            return None
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
        return self.cache_dir / f"{digest}{suffix}"

    def _cache_read(self, url: str, *, suffix: str = ".json") -> Any | None:
        path = self._cache_path(url, suffix=suffix)
        if path is None or not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def _cache_write(self, url: str, payload: Any, *, suffix: str = ".json") -> None:
        path = self._cache_path(url, suffix=suffix)
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")

    def _text_cache_path(self, url: str) -> Path | None:
        return self._cache_path(url, suffix=".txt")

    def _cache_read_text(self, url: str) -> str | None:
        path = self._text_cache_path(url)
        if path is None or not path.is_file():
            return None
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return None

    def _cache_write_text(self, url: str, body: str) -> None:
        path = self._text_cache_path(url)
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: D102
        return None


# Sentinel cached in place of a log body that has aged out, so a re-run is
# byte-stable instead of re-asking GitHub for something known to be gone.
_TEXT_GONE = "\x00GONE\x00"
