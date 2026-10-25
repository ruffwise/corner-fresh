"""Archive maintenance: redaction of already-archived personal bodies, repair of
responses damaged by redact-v1, integrity verification, and test-fixture export.

archive-scrub     Rewrites envelopes from PERSONAL endpoints only (schema-enforced),
                  after a dry run that prints a per-endpoint summary. --apply needs
                  --expect N equal to the dry-run count. Originals are MOVED to
                  data/superseded/ (outside the archive, so never synced off-box),
                  not deleted, and the difference is verified to be exactly the
                  redacted personal fields.
archive-finalize  Deletes superseded originals, only for named raw ids, only after
                  re-verifying the rewritten copy, and records who confirmed it.
damaged / refetch Finds responses whose DATA was altered by redact-v1 (from the
                  envelopes' own redaction records and from archive_rewrite) and
                  re-fetches each as a new raw row linked in raw_refetch.
archive-verify    Checks every raw row against the file it resolves to.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import sqlite3
import urllib.parse
from collections import Counter, defaultdict
from pathlib import Path

from .redact import MARK, PERSONAL_ENDPOINTS, POLICY_VERSION, redact
from .store import Archive, now_ms


def _personal_fields_only(old: object, new: object, path: str = "$") -> list[str] | None:
    """Paths where old and new differ. Returns None if any difference is not a
    replacement by MARK (a rewrite must change nothing but redacted values)."""
    if isinstance(old, dict) and isinstance(new, dict):
        if old.keys() != new.keys():
            return None
        out = []
        for k in old:
            d = _personal_fields_only(old[k], new[k], f"{path}.{k}")
            if d is None:
                return None
            out += d
        return out
    if isinstance(old, list) and isinstance(new, list):
        if len(old) != len(new):
            return None
        out = []
        for i, (a, b) in enumerate(zip(old, new)):
            d = _personal_fields_only(a, b, f"{path}[{i}]")
            if d is None:
                return None
            out += d
        return out
    if old == new:
        return []
    return [path] if new == MARK or (isinstance(new, str) and MARK in new) else None


def _candidates(con, archive: Archive, secrets):
    rows = con.execute(
        "SELECT l.raw_id, l.archive_path, l.body_sha256, a.provider, a.endpoint FROM v_archive_location l"
        " JOIN raw_response r ON r.raw_id = l.raw_id JOIN api_request a ON a.request_id = r.request_id"
        " WHERE l.rewritten = 0 ORDER BY l.raw_id").fetchall()
    for raw_id, path, body_sha, provider, endpoint in rows:
        env = Archive.read(Path(path))
        red = redact(env["body"].encode("utf-8"), secrets, provider, endpoint)
        if red.paths:
            yield raw_id, path, body_sha, provider, endpoint, env, red


def scrub(con: sqlite3.Connection, archive: Archive, secrets: tuple[str, ...], apply: bool,
          expect: int | None = None, superseded_dir: Path | None = None) -> dict:
    found = list(_candidates(con, archive, secrets))
    personal = [c for c in found if (c[3], c[4]) in PERSONAL_ENDPOINTS]
    refused = [c for c in found if (c[3], c[4]) not in PERSONAL_ENDPOINTS]
    summary = defaultdict(Counter)
    for c in found:
        for rule, n in c[6].rules.items():
            summary[f"{c[3]} {c[4]}"][rule] += n
    result = {"policy": POLICY_VERSION, "dry_run": not apply,
              "rewrite_candidates": len(personal),
              "per_endpoint": {k: dict(v) for k, v in sorted(summary.items())},
              "refused_data_endpoint_records": [{"raw_id": c[0], "endpoint": f"{c[3]} {c[4]}",
                                                 "rules": dict(c[6].rules)} for c in refused],
              "rewritten": 0}
    if refused:
        result["warning"] = ("records on DATA endpoints contain a live secret; they are never rewritten. "
                             "Stop and report them.")
    if not apply:
        result["next"] = f"if the per-endpoint summary is what you expect, rerun with --apply --expect {len(personal)}"
        return result
    if expect is None or expect != len(personal):
        raise RuntimeError(f"--expect {expect} does not match {len(personal)} rewrite candidates; nothing changed")
    if superseded_dir is None:
        raise RuntimeError("superseded_dir required")
    superseded_dir.mkdir(parents=True, exist_ok=True)
    for raw_id, path, body_sha, provider, endpoint, env, red in personal:
        old_body = json.loads(env["body"])
        new_body = json.loads(red.body)
        diff = _personal_fields_only(old_body, new_body)
        if diff is None or sorted(diff) != sorted(red.paths):
            raise RuntimeError(f"raw {raw_id}: rewrite would change more than the redacted fields; stopped")
        new_env = dict(env)
        new_env["body"] = red.body.decode("utf-8", "replace")
        new_env["body_sha256"] = hashlib.sha256(red.body).hexdigest()
        new_env["redaction"] = {"policy": POLICY_VERSION, "paths": red.paths, "reserialised": red.reserialised,
                                "rules": dict(red.rules), "rewritten_from": Path(path).name,
                                "rewritten_at_ms": now_ms()}
        _, new_path = archive.write(new_env)
        if Archive.read(new_path)["body"] != new_env["body"]:
            raise RuntimeError(f"raw {raw_id}: rewritten copy did not read back identically; original kept")
        dest = superseded_dir / Path(path).name
        con.execute("BEGIN")
        try:
            con.execute("INSERT INTO archive_rewrite VALUES (?,?,?,?,?,?,?,?)",
                        (raw_id, path, str(new_path), body_sha, new_env["body_sha256"], POLICY_VERSION,
                         json.dumps(red.paths), now_ms()))
            con.execute("INSERT INTO archive_superseded (raw_id, superseded_path, moved_at_ms, verified_diff)"
                        " VALUES (?,?,?,?)", (raw_id, str(dest), now_ms(), json.dumps(diff)))
            Path(path).replace(dest)          # move, never delete, until archive-finalize
        except BaseException:
            con.execute("ROLLBACK")
            raise
        con.execute("COMMIT")
        result["rewritten"] += 1
    result["next"] = ("originals are in the superseded folder; after checking, run "
                      "archive-finalize --raw-ids <ids> --confirmed-by <your name>")
    return result


def finalize(con: sqlite3.Connection, raw_ids: list[int], confirmed_by: str) -> dict:
    if not confirmed_by.strip():
        raise RuntimeError("--confirmed-by is required")
    done = []
    for raw_id in raw_ids:
        row = con.execute("SELECT s.superseded_path, w.new_archive_path, w.new_body_sha256, s.deleted_at_ms"
                          " FROM archive_superseded s JOIN archive_rewrite w ON w.raw_id = s.raw_id"
                          " WHERE s.raw_id=?", (raw_id,)).fetchone()
        if row is None:
            raise RuntimeError(f"raw {raw_id}: no superseded original recorded")
        sup, new_path, new_sha, deleted = row
        if deleted is not None:
            continue
        env = Archive.read(Path(new_path))
        if hashlib.sha256(env["body"].encode("utf-8")).hexdigest() != new_sha:
            raise RuntimeError(f"raw {raw_id}: rewritten copy fails its hash; original kept")
        Path(sup).unlink(missing_ok=True)
        con.execute("UPDATE archive_superseded SET deleted_at_ms=?, confirmed_by=? WHERE raw_id=?",
                    (now_ms(), confirmed_by, raw_id))
        done.append(raw_id)
    return {"originals_deleted": done}


# --- damage from redact-v1 and its repair ---------------------------------------------------------

def damaged(con: sqlite3.Connection) -> list[dict]:
    """Responses on DATA endpoints whose stored body was altered by redaction.
    Evidence: (a) archive_rewrite rows on data endpoints (the v0.5 scrub);
              (b) envelopes whose own redaction record lists changed paths."""
    out = {}
    for raw_id, paths in con.execute(
            "SELECT w.raw_id, w.redacted_paths FROM archive_rewrite w JOIN raw_response r ON r.raw_id = w.raw_id"
            " JOIN api_request a ON a.request_id = r.request_id"
            " WHERE NOT EXISTS (SELECT 1 FROM personal_endpoint p WHERE p.provider=a.provider AND p.endpoint=a.endpoint)"):
        out[raw_id] = {"raw_id": raw_id, "evidence": "archive_rewrite", "paths": json.loads(paths)}
    # Only envelopes written while redact-v1 was live can carry v1 damage: from the
    # start of 25 Oct 2026 (v0.5 deploy day; v0.4 had no redaction) to migration 006.
    v1_start = 1792886400000
    v1_end = con.execute("SELECT applied_at_ms FROM schema_event WHERE user_version = 6").fetchone()[0]
    for raw_id, path, provider, endpoint in con.execute(
            "SELECT l.raw_id, l.archive_path, a.provider, a.endpoint FROM v_archive_location l"
            " JOIN raw_response r ON r.raw_id = l.raw_id JOIN api_request a ON a.request_id = r.request_id"
            " WHERE l.rewritten = 0 AND r.received_at_ms BETWEEN ? AND ?", (v1_start, v1_end)):
        if (provider, endpoint) in PERSONAL_ENDPOINTS or raw_id in out:
            continue
        red = (Archive.read(Path(path)).get("redaction") or {})
        if red.get("paths") and red.get("policy") == "redact-v1":
            out[raw_id] = {"raw_id": raw_id, "evidence": f"envelope {red.get('policy')}", "paths": red["paths"]}
    linked = {r[0] for r in con.execute("SELECT damaged_raw_id FROM raw_refetch")}
    rows = []
    for raw_id, d in sorted(out.items()):
        if raw_id in linked:
            continue
        prov, ep, params = con.execute(
            "SELECT a.provider, a.endpoint, a.params_canonical FROM raw_response r JOIN api_request a"
            " ON a.request_id = r.request_id WHERE r.raw_id=?", (raw_id,)).fetchone()
        patterns = Counter(_pattern(p) for p in d["paths"])
        rows.append({"raw_id": raw_id, "provider": prov, "endpoint": ep, "params": params,
                     "evidence": d["evidence"], "changed_values": len(d["paths"]), "patterns": dict(patterns)})
    return rows


def _pattern(path: str) -> str:
    import re
    return re.sub(r"\[\d+\]", "[*]", path)


def refetch(con: sqlite3.Connection, af, slot, rows: list[dict]) -> dict:
    """Re-fetch each damaged response with the current (v2) redaction; link old to new."""
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for d in rows:
        if d["provider"] != "api_football":
            raise RuntimeError(f"raw {d['raw_id']}: re-fetch implemented for api-football only")
        groups[(d["endpoint"], d["params"])].append(d)
    linked = 0
    for (endpoint, params_c), ds in sorted(groups.items()):
        params = dict(urllib.parse.parse_qsl(params_c))
        params.pop("page", None)
        res = af.call(slot, endpoint, params, f"re-fetch of damaged raw {[d['raw_id'] for d in ds]}")
        req = con.execute("SELECT request_id, status FROM api_request WHERE slot_id=? AND endpoint=? AND params_canonical=?",
                          (slot.slot_id, endpoint, params_c)).fetchone()
        if not res or req is None or req[1] != "complete":
            continue
        for d in ds:
            con.execute("INSERT INTO raw_refetch VALUES (?,?,?,?)",
                        (d["raw_id"], req[0], json.dumps({"evidence": d["evidence"], "patterns": d["patterns"]}),
                         now_ms()))
            linked += 1
    return {"damaged_rows": len(rows), "distinct_requests": len(groups), "linked": linked}


# --- integrity --------------------------------------------------------------------------------------

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
    pending = con.execute("SELECT raw_id, superseded_path FROM archive_superseded WHERE deleted_at_ms IS NULL").fetchall()
    for raw_id, sup in pending:
        if not Path(sup).exists():
            problems.append((raw_id, "superseded original missing before finalize"))
    unrepaired = len(damaged(con))
    return {"checked": n, "problems": problems[:50], "problem_count": len(problems),
            "superseded_originals_pending": [r[0] for r in pending], "damaged_unrepaired": unrepaired}


# --- real-payload test fixtures --------------------------------------------------------------------

REQUIRED_ENDPOINTS = [
    ("api_football", "/status"), ("api_football", "/leagues"), ("api_football", "/fixtures"),
    ("api_football", "/fixtures?ids"),          # match detail (players, passes.key): the v0.5 false positive
    ("api_football", "/fixtures/statistics"), ("api_football", "/odds"), ("api_football", "/odds/bookmakers"),
    ("api_football", "/odds/bets"),
    ("oddspapi", "/v4/account"), ("oddspapi", "/v4/fixtures"), ("oddspapi", "/v4/historical-odds"),
]


def slug(endpoint: str) -> str:
    return (endpoint.strip("/").replace("v4/", "").replace("/", "_").replace("?", "_by_")) or "root"


def export_fixtures(con: sqlite3.Connection, out_dir: Path, secrets: tuple[str, ...],
                    history_kickoffs: dict[str, str], per_endpoint: int = 2) -> dict:
    """Copy recent usable real responses into test fixtures, redacted under the current
    policy. Never exports a response damaged by redact-v1 (its data is not real)."""
    written, missing = [], []
    bad = {d["raw_id"] for d in damaged(con)} | {r[0] for r in con.execute("SELECT damaged_raw_id FROM raw_refetch")}
    bad |= {r[0] for r in con.execute(
        "SELECT w.raw_id FROM archive_rewrite w JOIN raw_response r ON r.raw_id=w.raw_id JOIN api_request a"
        " ON a.request_id=r.request_id WHERE NOT EXISTS (SELECT 1 FROM personal_endpoint p"
        " WHERE p.provider=a.provider AND p.endpoint=a.endpoint)")}
    for provider, ep_key in REQUIRED_ENDPOINTS:
        endpoint, _, only = ep_key.partition("?")
        rows = con.execute(
            "SELECT r.raw_id, a.params_canonical, l.archive_path FROM raw_response r"
            " JOIN api_request a ON a.request_id = r.request_id JOIN v_archive_location l ON l.raw_id = r.raw_id"
            " WHERE a.provider=? AND a.endpoint=? AND r.usable=1 ORDER BY r.raw_id DESC LIMIT 200",
            (provider, endpoint)).fetchall()
        kept = 0
        for raw_id, params_c, path in rows:
            if kept >= per_endpoint:
                break
            params = dict(urllib.parse.parse_qsl(params_c))
            if raw_id in bad or (only and only not in params) or (not only and endpoint == "/fixtures" and "ids" in params):
                continue
            env = Archive.read(Path(path))
            if env.get("redaction", {}).get("policy") == "redact-v1" and (provider, endpoint) not in PERSONAL_ENDPOINTS \
                    and env["redaction"].get("paths"):
                continue
            red = redact(env["body"].encode("utf-8"), secrets, provider, endpoint)
            body = json.loads(red.body)
            if env.get("page", 1) > 1:
                params["page"] = str(env["page"])
            meta = {"provider": provider, "endpoint": endpoint, "params": params, "raw_id": raw_id,
                    "received_at_ms": env["received_at_ms"], "redacted_paths": red.paths, "policy": POLICY_VERSION}
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
            f = d / f"{slug(ep_key)}_{raw_id}.json.gz"
            f.write_bytes(gzip.compress(json.dumps({"meta": meta, "body": body}, sort_keys=True).encode(), mtime=0))
            written.append(str(f))
            kept += 1
        if kept == 0:
            missing.append(f"{provider} {ep_key}")
    leaked = [w for w in written if any(s and s.encode() in gzip.decompress(Path(w).read_bytes()) for s in secrets)]
    if leaked:
        for w in leaked:
            Path(w).unlink()
        raise RuntimeError(f"export aborted: a live secret survived redaction in {leaked}")
    return {"written": written, "missing_endpoints": missing}
