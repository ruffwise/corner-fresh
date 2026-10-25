"""Archive maintenance: redaction of already-archived bodies, and integrity verification.

archive-scrub  rewrites envelopes whose body still contains secrets or personal
               data (archived before redaction existed). It is the one permitted
               rewrite. The raw_response row is untouched; archive_rewrite records
               old and new path and hash, and v_archive_location resolves reads.
               Order is crash-safe: write the new file, record it, then delete
               the old file.
archive-verify checks every raw_response row against the file it resolves to.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import sqlite3
from pathlib import Path

from .redact import POLICY_VERSION, redact
from .store import Archive, now_ms


def scrub(con: sqlite3.Connection, archive: Archive, secrets: tuple[str, ...], apply: bool) -> dict:
    rows = con.execute("SELECT l.raw_id, l.archive_path, l.body_sha256 FROM v_archive_location l"
                       " WHERE l.rewritten = 0 ORDER BY l.raw_id").fetchall()
    found, done = [], 0
    for raw_id, path, body_sha in rows:
        env = Archive.read(Path(path))
        body = env["body"].encode("utf-8")
        red = redact(body, secrets)
        if not red.paths:
            continue
        found.append({"raw_id": raw_id, "paths": red.paths})
        if not apply:
            continue
        new_env = dict(env)
        new_env["body"] = red.body.decode("utf-8", "replace")
        new_env["body_sha256"] = hashlib.sha256(red.body).hexdigest()
        new_env["redaction"] = {"policy": POLICY_VERSION, "paths": red.paths, "reserialised": red.reserialised,
                                "rewritten_from": Path(path).name, "rewritten_at_ms": now_ms()}
        _, new_path = archive.write(new_env)
        con.execute("INSERT INTO archive_rewrite VALUES (?,?,?,?,?,?,?,?)",
                    (raw_id, path, str(new_path), body_sha, new_env["body_sha256"], POLICY_VERSION,
                     json.dumps(red.paths), now_ms()))
        Path(path).unlink(missing_ok=True)
        done += 1
    return {"needing_redaction": len(found), "rewritten": done, "dry_run": not apply,
            "items": [{"raw_id": f["raw_id"], "paths": f["paths"]} for f in found[:50]]}


def verify(con: sqlite3.Connection, secrets: tuple[str, ...] = ()) -> dict:
    problems = []
    n = 0
    for raw_id, path, body_sha in con.execute("SELECT raw_id, archive_path, body_sha256 FROM v_archive_location"):
        n += 1
        p = Path(path)
        if not p.exists():
            problems.append((raw_id, "file missing"))
            continue
        data = gzip.decompress(p.read_bytes())
        if hashlib.sha256(data).hexdigest() != p.name.split(".")[0]:
            problems.append((raw_id, "file name is not the hash of its content"))
            continue
        env = json.loads(data)
        if hashlib.sha256(env["body"].encode("utf-8")).hexdigest() != body_sha:
            problems.append((raw_id, "body hash differs from the database"))
        if secrets and any(s and s in data.decode("utf-8", "replace") for s in secrets):
            problems.append((raw_id, "a live secret is present in the archived file"))
    return {"checked": n, "problems": problems[:50], "problem_count": len(problems)}


# --- real-payload test fixtures --------------------------------------------------------------

REQUIRED_ENDPOINTS = [
    ("api_football", "/status"), ("api_football", "/leagues"), ("api_football", "/fixtures"),
    ("api_football", "/fixtures/statistics"), ("api_football", "/odds"), ("api_football", "/odds/bookmakers"),
    ("api_football", "/odds/bets"),
    ("oddspapi", "/v4/account"), ("oddspapi", "/v4/fixtures"), ("oddspapi", "/v4/historical-odds"),
]


def slug(endpoint: str) -> str:
    return endpoint.strip("/").replace("/v4/", "").replace("v4/", "").replace("/", "_") or "root"


def export_fixtures(con: sqlite3.Connection, out_dir: Path, secrets: tuple[str, ...],
                    history_kickoffs: dict[str, str], per_endpoint: int = 2) -> dict:
    """Copy recent usable real responses into test fixtures, re-redacted (defence in depth).

    History payloads need a scheduled kickoff to test the close rule; pass them
    as {oddspapi_fixture_id: ISO time} or they are taken from the candidates.
    """
    import urllib.parse
    written, missing = [], []
    for provider, endpoint in REQUIRED_ENDPOINTS:
        rows = con.execute(
            "SELECT r.raw_id, a.params_canonical, l.archive_path FROM raw_response r"
            " JOIN api_request a ON a.request_id = r.request_id JOIN v_archive_location l ON l.raw_id = r.raw_id"
            " WHERE a.provider=? AND a.endpoint=? AND r.usable=1 ORDER BY r.raw_id DESC LIMIT 50",
            (provider, endpoint)).fetchall()
        kept = 0
        for raw_id, params_c, path in rows:
            if kept >= per_endpoint:
                break
            env = Archive.read(Path(path))
            red = redact(env["body"].encode("utf-8"), secrets)
            body = json.loads(red.body)
            params = dict(urllib.parse.parse_qsl(params_c))
            if env.get("page", 1) > 1:
                params["page"] = str(env["page"])
            meta = {"provider": provider, "endpoint": endpoint, "params": params, "raw_id": raw_id,
                    "received_at_ms": env["received_at_ms"], "redacted_paths": red.paths}
            if endpoint == "/v4/historical-odds":
                fid = params.get("fixtureId")
                ko = history_kickoffs.get(fid)
                if ko is None:
                    row = con.execute("SELECT start_ms FROM provider_fixture_candidate WHERE provider_fixture_id=?"
                                      " ORDER BY raw_id DESC LIMIT 1", (fid,)).fetchone()
                    if row:
                        from datetime import datetime, timezone
                        ko = datetime.fromtimestamp(row[0] / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                if ko is None:
                    continue
                meta["scheduled_kickoff_utc"] = ko
                meta["status"] = "FT"
            d = out_dir / provider
            d.mkdir(parents=True, exist_ok=True)
            f = d / f"{slug(endpoint)}_{raw_id}.json.gz"
            f.write_bytes(gzip.compress(json.dumps({"meta": meta, "body": body}, sort_keys=True).encode(), mtime=0))
            written.append(str(f))
            kept += 1
        if kept == 0:
            missing.append(f"{provider} {endpoint}")
    leaked = [w for w in written if any(s and s.encode() in gzip.decompress(Path(w).read_bytes()) for s in secrets)]
    if leaked:
        for w in leaked:
            Path(w).unlink()
        raise RuntimeError(f"export aborted: a live secret survived redaction in {leaked}")
    return {"written": written, "missing_endpoints": missing}
