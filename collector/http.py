"""HTTP fetching with archive-first recording.

Every attempt that reaches the network produces one archive envelope and one
raw_response row, including errors and network failures (http_status 0).
"""
from __future__ import annotations

import email.utils
import hashlib
import json
import socket
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable, Protocol

from .redact import POLICY_VERSION, redact, redact_text
from .store import Archive, now_ms

SECRET_PARAM_NAMES = {"apikey", "api_key", "key", "token"}
DROP_RESPONSE_HEADERS = {"set-cookie"}


class Transport(Protocol):
    def __call__(self, url: str, headers: dict[str, str], timeout: float) -> tuple[int, dict[str, str], bytes]:
        ...


def urllib_transport(url: str, headers: dict[str, str], timeout: float) -> tuple[int, dict[str, str], bytes]:
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in (e.headers or {}).items()}, e.read() or b""


@dataclass
class Result:
    raw_id: int
    status: int
    body: object | None          # parsed JSON or None
    usable: bool
    headers: dict[str, str]


def canonical(params: dict) -> str:
    for k in params:
        if k.lower() in SECRET_PARAM_NAMES:
            raise ValueError(f"secret parameter {k!r} must not be part of canonical params")
    return urllib.parse.urlencode(sorted((k, str(v)) for k, v in params.items()))


def parse_date_ms(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return int(email.utils.parsedate_to_datetime(value).timestamp() * 1000)
    except (TypeError, ValueError):
        return None


class Fetcher:
    def __init__(self, con: sqlite3.Connection, archive: Archive, transport: Transport = urllib_transport,
                 clock: Callable[[], int] = now_ms, sleep: Callable[[float], None] = time.sleep,
                 secrets: tuple[str, ...] = ()):
        self.secrets = tuple(s for s in secrets if s)
        self.con = con
        self.archive = archive
        self.transport = transport
        self.clock = clock
        self.sleep = sleep
        self._last_call: dict[str, float] = {}

    def request_row(self, slot_id: int, provider: str, endpoint: str, params: dict,
                    expected_pages: int | None = None) -> int:
        params_c = canonical(params)
        row = self.con.execute(
            "SELECT request_id FROM api_request WHERE slot_id=? AND provider=? AND endpoint=? AND params_canonical=?",
            (slot_id, provider, endpoint, params_c)).fetchone()
        if row:
            return row[0]
        return self.con.execute(
            "INSERT INTO api_request (slot_id, provider, endpoint, params_canonical, expected_pages) VALUES (?,?,?,?,?)",
            (slot_id, provider, endpoint, params_c, expected_pages)).lastrowid

    def pace(self, key: str, min_interval_s: float) -> None:
        last = self._last_call.get(key)
        if last is not None:
            wait = min_interval_s - (time.monotonic() - last)
            if wait > 0:
                self.sleep(wait)
        self._last_call[key] = time.monotonic()

    def get(self, *, request_id: int, provider: str, paged: bool, url: str, params: dict,
            secret_params: dict | None = None, secret_headers: dict | None = None,
            page: int = 1, attempt: int = 1, timeout: float = 60.0,
            errors_of: Callable[[object], str] | None = None) -> Result:
        """One HTTP attempt. Secrets go only into the outgoing request."""
        query = dict(params)
        query.update(secret_params or {})
        full = url + ("?" + urllib.parse.urlencode(query) if query else "")
        headers = {"Accept": "application/json", "User-Agent": "corners-collector/0.4"}
        headers.update(secret_headers or {})
        requested = self.clock()
        try:
            status, rheaders, body = self.transport(full, headers, timeout)
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError) as e:
            status, rheaders, body = 0, {}, json.dumps({"network_error": repr(e)}).encode()
        received = self.clock()
        rheaders = {k: v for k, v in rheaders.items() if k not in DROP_RESPONSE_HEADERS}
        # The one permitted change to a raw body: redact secrets and personal data
        # BEFORE hashing, archiving or parsing (collector/redact.py). The endpoint
        # comes from the api_request row, so the scope rule cannot be mis-passed.
        req_provider, req_endpoint = self.con.execute(
            "SELECT provider, endpoint FROM api_request WHERE request_id=?", (request_id,)).fetchone()
        red = redact(body, self.secrets, req_provider, req_endpoint)
        body = red.body
        rheaders = {k: redact_text(v, self.secrets) for k, v in rheaders.items()}
        parsed = None
        try:
            parsed = json.loads(body) if body else None
        except ValueError:
            parsed = None
        if status == 200 and parsed is not None:
            api_errors = errors_of(parsed) if errors_of else "[]"
        elif status == 0:
            api_errors = None
        else:
            api_errors = json.dumps({"http_status": status})
        results_count = paging_current = paging_total = None
        if paged and isinstance(parsed, dict):
            results_count = parsed.get("results")
            pg = parsed.get("paging") or {}
            paging_current, paging_total = pg.get("current"), pg.get("total")
        envelope = {
            "v": 1, "provider": provider, "url": url, "params": params, "page": page, "attempt": attempt,
            "requested_at_ms": requested, "received_at_ms": received, "http_status": status,
            "response_headers": rheaders, "body_sha256": hashlib.sha256(body).hexdigest(),
            "redaction": {"policy": POLICY_VERSION, "paths": red.paths, "reserialised": red.reserialised,
                          "rules": dict(red.rules)},
            "body": body.decode("utf-8", errors="replace"),
        }
        _, path = self.archive.write(envelope)
        raw_id = self.con.execute(
            "INSERT INTO raw_response (request_id, page, attempt, paged, sha256, archive_path, http_status,"
            " requested_at_ms, received_at_ms, server_date_ms, body_bytes, api_errors_json, results_count,"
            " paging_current, paging_total) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (request_id, page, attempt, 1 if paged else 0, envelope["body_sha256"], str(path), status,
             requested, received, parse_date_ms(rheaders.get("date")), len(body), api_errors,
             results_count, paging_current, paging_total)).lastrowid
        for rule, n in sorted(red.rules.items()):
            self.con.execute("INSERT INTO raw_redaction VALUES (?,?,?,?)", (raw_id, rule, n, POLICY_VERSION))
        usable = self.con.execute("SELECT usable FROM raw_response WHERE raw_id=?", (raw_id,)).fetchone()[0] == 1
        return Result(raw_id, status, parsed, usable, rheaders)
