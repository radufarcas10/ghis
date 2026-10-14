"""A small JSON-over-HTTP client for the hosting APIs."""

from __future__ import annotations

import json
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


class ApiError(Exception):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class SameHostRedirect(urllib.request.HTTPRedirectHandler):
    """Follow redirects only within one host, so an allowed API cannot bounce a hosted
    server (or a token) to somewhere else."""

    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> Any:
        if urllib.parse.urlparse(newurl).hostname != urllib.parse.urlparse(req.full_url).hostname:
            raise ApiError(f"refusing a redirect to another host ({newurl})")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class Http:
    def __init__(
        self, base: str, headers: dict[str, str], insecure: bool, timeout: float = 60
    ) -> None:
        self.base = base.rstrip("/")
        self.headers = headers
        self.timeout = timeout
        context = ssl._create_unverified_context() if insecure else None
        self.opener = urllib.request.build_opener(
            SameHostRedirect, urllib.request.HTTPSHandler(context=context)
        )

    def request(
        self, path: str, params: dict[str, Any] | None = None, body: Any = None
    ) -> tuple[Any, Any]:
        url = path if path.startswith("http") else self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body).encode() if body is not None else None
        headers = {**self.headers, "Accept": "application/json"}
        if data:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers)
        for attempt in range(6):
            try:
                with self.opener.open(request, timeout=self.timeout) as resp:
                    return json.load(resp), resp.headers
            except urllib.error.HTTPError as err:
                if err.code == 429 or err.code >= 500:
                    time.sleep(int(err.headers.get("Retry-After") or 2**attempt))
                    continue
                raise ApiError(f"{err.code} {err.reason} on {path}", err.code) from None
            except urllib.error.URLError as err:
                if attempt < 2 and self.timeout >= 30:
                    time.sleep(2**attempt)
                    continue
                raise ApiError(f"cannot reach {self.base}: {err.reason}") from None
        raise ApiError(f"gave up on {path} after repeated server errors")
