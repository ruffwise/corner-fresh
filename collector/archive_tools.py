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
