"""Redaction (policy redact-v2): secrets and personal data never reach the archive
or stdout, AND real data is never altered (the v0.5 failure: "key" matched
api-football's passes.key and blanked key passes in match-detail responses).

Every test asserts on bytes on disk or captured stdout, not intermediate objects.
"""
from __future__ import annotations

import email.utils
import gzip
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from collector import ledger  # noqa: E402
from collector.af import AF  # noqa: E402
from collector.archive_tools import damaged, export_fixtures, finalize, scrub, verify  # noqa: E402
from collector.redact import MARK, Redaction, redact  # noqa: E402
from collector.store import Archive, now_ms  # noqa: E402
from test_collector import AF_KEY, OP_KEY, af_fixture, af_page, env, patched_main, run  # noqa: E402,F401

EMAIL = "someone@example.com"


def archive_bytes(cfg) -> bytes:
    return b"".join(gzip.decompress(p.read_bytes()) for p in cfg.archive_dir.rglob("*.json.gz"))


def match_detail(fid=1001, key_passes=(2, 0, 5)):
    """The shape of /fixtures?ids=... that redact-v1 damaged (from the VM scrub output)."""
    players = [{"player": {"id": 100 + i, "name": f"Player {i}"},
                "statistics": [{"games": {"minutes": 90}, "passes": {"total": 40, "key": k, "accuracy": "88"}}]}
               for i, k in enumerate(key_passes)]
    it = af_fixture(fid, (now_ms() - 30 * 3_600_000) // 1000 * 1000, status="FT", ft=(1, 0))
    it["players"] = [{"team": {"id": 1, "name": "Home FC"}, "players": players}]
    return af_page([it])


# --- personal endpoints: redacted -----------------------------------------------------------------

def test_op_account_key_and_email_never_stored_or_printed(env, monkeypatch, capsys):
    cfg, con, fake, f = env
    body = {"api_key": OP_KEY, "email": EMAIL, "request_limit": 250, "request_count": 7, "plan": "free"}
    fake.add("/v4/account", {}, (200, body))
    main = patched_main(monkeypatch, fake)
    rc = main(["--config", str(cfg.db_path.parent.parent / "config.toml"), "op-account"])
    printed = capsys.readouterr()
    blob = archive_bytes(cfg)
    assert rc == 0
    for secret in (OP_KEY, EMAIL):
        assert secret.encode() not in blob and secret not in printed.out + printed.err, secret
    env_ = Archive.read(next(cfg.archive_dir.rglob("*.json.gz")))
    assert json.loads(env_["body"]) == {**body, "api_key": MARK, "email": MARK}
    assert env_["redaction"]["policy"] == "redact-v2"
    rules = dict(con.execute("SELECT rule, n FROM raw_redaction").fetchall())
    assert rules == {"field:api_key": 1, "field:email": 1}


def test_af_status_personal_fields_redacted(env):
    cfg, con, fake, f = env
    status = {"account": {"firstname": "Ada", "lastname": "Obi", "email": EMAIL},
              "subscription": {"plan": "Pro", "active": True}, "requests": {"current": 12, "limit_day": 7500}}
    fake.add("/status", {}, (200, af_page(status)))
    res = run(con, "af_bootstrap", lambda s: AF(cfg, con, f).call(s, "/status", {}, "status"))
    for v in ("Ada", "Obi", EMAIL):
        assert v.encode() not in archive_bytes(cfg)
    assert res[0].body["response"]["subscription"]["plan"] == "Pro"


# --- data endpoints: never altered (the regression) -------------------------------------------------

def test_match_detail_with_key_passes_is_byte_identical(env):
    cfg, con, fake, f = env
    body = match_detail()
    raw_bytes = json.dumps(body).encode()
    fake.add("/fixtures", {"ids": "1001"}, (200, body))
    run(con, "af_results", lambda s: AF(cfg, con, f).call(s, "/fixtures", {"ids": "1001"}, "detail"))
    env_ = Archive.read(next(cfg.archive_dir.rglob("*.json.gz")))
    assert env_["body"].encode() == raw_bytes                       # byte-identical
    assert env_["body_sha256"] == hashlib.sha256(raw_bytes).hexdigest()
    assert env_["redaction"]["paths"] == [] and not env_["redaction"]["reserialised"]
    assert [p["statistics"][0]["passes"]["key"] for p in json.loads(env_["body"])["response"][0]["players"][0]["players"]] \
        == [2, 0, 5]
    assert con.execute("SELECT count(*) FROM raw_redaction").fetchone()[0] == 0


@pytest.mark.parametrize("name", ["key", "token", "name", "phone", "username", "secret", "password"])
def test_generic_names_on_data_endpoints_untouched(name):
    body = json.dumps({"response": [{"stats": {name: "value-123"}, "teams": {"home": {"name": "Arsenal"}}}]}).encode()
    r = redact(body, (AF_KEY, OP_KEY), "api_football", "/fixtures")
    assert r.body is body and r.paths == []


def test_personal_looking_field_on_data_endpoint_is_detected_not_altered(env, monkeypatch, capsys):
    cfg, con, fake, f = env
    body = af_page([{"venue": {"name": "Ground", "email": "box@club.example"}}])
    raw_bytes = json.dumps(body).encode()
    fake.add("/fixtures", {"league": 39, "season": 2026}, (200, body))
    run(con, "af_fixtures", lambda s: AF(cfg, con, f).call(s, "/fixtures", {"league": 39, "season": 2026}, "x"))
    assert Archive.read(next(cfg.archive_dir.rglob("*.json.gz")))["body"].encode() == raw_bytes
    assert con.execute("SELECT rule, n FROM raw_redaction").fetchall() == [("detect:email", 1)]
    from collector.__main__ import main
    rc = main(["--config", str(cfg.db_path.parent.parent / "config.toml"), "watchdog"])
    assert rc == 1 and "redaction_on_data_endpoint:api_football /fixtures:detect:email" in capsys.readouterr().out


def test_live_key_echoed_into_data_is_scrubbed_and_alerts(env, monkeypatch, capsys):
    cfg, con, fake, f = env
    fake.add("/fixtures", {"league": 39, "season": 2026}, (200, af_page([{"note": f"debug {AF_KEY}"}])))
    run(con, "af_fixtures", lambda s: AF(cfg, con, f).call(s, "/fixtures", {"league": 39, "season": 2026}, "x"))
    assert AF_KEY.encode() not in archive_bytes(cfg)
    assert con.execute("SELECT rule FROM raw_redaction").fetchall() == [("value:live_secret",)]
    from collector.__main__ import main
    assert main(["--config", str(cfg.db_path.parent.parent / "config.toml"), "watchdog"]) == 1


def test_schema_refuses_field_rule_on_data_endpoint(env):
    cfg, con, fake, f = env
    fake.add("/fixtures", {"ids": "1001"}, (200, match_detail()))
    run(con, "af_results", lambda s: AF(cfg, con, f).call(s, "/fixtures", {"ids": "1001"}, "detail"))
    raw_id = con.execute("SELECT max(raw_id) FROM raw_response").fetchone()[0]
    with pytest.raises(sqlite3.IntegrityError, match="field rule fired on a data endpoint"):
        con.execute("INSERT INTO raw_redaction VALUES (?, 'field:key', 1, 'redact-v2')", (raw_id,))


def test_secret_in_non_json_error_body_is_scrubbed(env):
    cfg, con, fake, f = env
    f.transport = lambda url, headers, timeout: (401, {"date": email.utils.formatdate(usegmt=True)},
                                                 f"bad key {AF_KEY}".encode())
    with pytest.raises(ledger.SlotIncomplete):
        run(con, "af_bootstrap", lambda s: AF(cfg, con, f).call(s, "/status", {}, "status"))
    assert AF_KEY.encode() not in archive_bytes(cfg)


def test_response_header_echoing_key_is_scrubbed(env):
    cfg, con, fake, f = env
    f.transport = lambda url, headers, timeout: (200, {"date": email.utils.formatdate(usegmt=True),
                                                       "x-debug": f"key={AF_KEY}"}, json.dumps(af_page({"ok": 1})).encode())
    run(con, "af_bootstrap", lambda s: AF(cfg, con, f).call(s, "/status", {}, "status"))
    assert AF_KEY.encode() not in archive_bytes(cfg)


# --- archive-scrub v2 ------------------------------------------------------------------------------

def no_redaction(mp):
    """Reproduce v0.4 behaviour (no redaction at all) to build a pre-fix archive."""
    import collector.http as h
    mp.setattr(h, "redact", lambda body, secrets, provider, endpoint: Redaction(body))


def v04_archive(env, monkeypatch):
    cfg, con, fake, f = env
    fake.add("/status", {}, (200, af_page({"account": {"firstname": "Ada", "email": EMAIL}, "subscription": {"plan": "Pro"}})))
    fake.add("/fixtures", {"ids": "1001"}, (200, match_detail()))
    with pytest.MonkeyPatch.context() as mp:
        no_redaction(mp)
        run(con, "af_bootstrap", lambda s: AF(cfg, con, f).call(s, "/status", {}, "status"))
        run(con, "af_results", lambda s: AF(cfg, con, f).call(s, "/fixtures", {"ids": "1001"}, "detail"))


def test_scrub_dry_run_lists_only_personal_endpoints(env, monkeypatch):
    v04_archive(env, monkeypatch)
    cfg, con, fake, f = env
    r = scrub(con, Archive(cfg.archive_dir), (AF_KEY, OP_KEY), apply=False)
    assert r["rewrite_candidates"] == 1
    assert list(r["per_endpoint"]) == ["api_football /status"]       # the match detail is not touched
    assert r["refused_data_endpoint_records"] == []


def test_scrub_apply_needs_matching_expect(env, monkeypatch):
    v04_archive(env, monkeypatch)
    cfg, con, fake, f = env
    for wrong in (None, 0, 5):
        with pytest.raises(RuntimeError, match="does not match"):
            scrub(con, Archive(cfg.archive_dir), (AF_KEY, OP_KEY), apply=True, expect=wrong,
                  superseded_dir=cfg.db_path.parent / "superseded")
    assert EMAIL.encode() in archive_bytes(cfg)                           # nothing changed


def test_scrub_moves_original_then_finalize_deletes(env, monkeypatch):
    v04_archive(env, monkeypatch)
    cfg, con, fake, f = env
    sup = cfg.db_path.parent / "superseded"
    r = scrub(con, Archive(cfg.archive_dir), (AF_KEY, OP_KEY), apply=True, expect=1, superseded_dir=sup)
    assert r["rewritten"] == 1
    assert EMAIL.encode() not in archive_bytes(cfg)                       # archive is clean ...
    originals = list(sup.glob("*.json.gz"))
    assert len(originals) == 1 and EMAIL.encode() in gzip.decompress(originals[0].read_bytes())   # ... original kept
    v = verify(con, (AF_KEY, OP_KEY))
    assert v["problem_count"] == 0 and len(v["superseded_originals_pending"]) == 1
    raw_id = v["superseded_originals_pending"][0]
    with pytest.raises(RuntimeError, match="confirmed-by"):
        finalize(con, [raw_id], " ")
    assert finalize(con, [raw_id], "Oluwanbe")["originals_deleted"] == [raw_id]
    assert not list(sup.glob("*.json.gz"))
    assert verify(con, (AF_KEY, OP_KEY))["superseded_originals_pending"] == []


def test_scrub_refuses_data_endpoint_with_live_secret(env, monkeypatch):
    cfg, con, fake, f = env
    fake.add("/fixtures", {"league": 39, "season": 2026}, (200, af_page([{"note": f"echo {AF_KEY}"}])))
    with pytest.MonkeyPatch.context() as mp:
        no_redaction(mp)
        run(con, "af_fixtures", lambda s: AF(cfg, con, f).call(s, "/fixtures", {"league": 39, "season": 2026}, "x"))
    r = scrub(con, Archive(cfg.archive_dir), (AF_KEY, OP_KEY), apply=False)
    assert r["rewrite_candidates"] == 0 and len(r["refused_data_endpoint_records"]) == 1 and "warning" in r


def test_schema_refuses_rewrite_of_data_record(env):
    cfg, con, fake, f = env
    fake.add("/fixtures", {"ids": "1001"}, (200, match_detail()))
    run(con, "af_results", lambda s: AF(cfg, con, f).call(s, "/fixtures", {"ids": "1001"}, "detail"))
    raw_id, path, sha = con.execute("SELECT raw_id, archive_path, sha256 FROM raw_response").fetchone()
    with pytest.raises(sqlite3.IntegrityError, match="data records are never rewritten"):
        con.execute("INSERT INTO archive_rewrite VALUES (?,?,?,?,?,?,?,?)",
                    (raw_id, path, path + "x", sha, "b" * 64, "redact-v2", "[]", now_ms()))


# --- damage from redact-v1, and its repair ----------------------------------------------------------

def v1_damage(env, monkeypatch):
    """Reproduce what v0.5 wrote on the VM: match detail with passes.key blanked, envelope policy redact-v1."""
    cfg, con, fake, f = env
    import collector.http as h

    def v1(body, secrets, provider, endpoint):
        obj = json.loads(body)
        paths = []
        for i, t in enumerate(obj["response"][0]["players"]):
            for j, p in enumerate(t["players"]):
                p["statistics"][0]["passes"]["key"] = MARK
                paths.append(f"$.response[0].players[{i}].players[{j}].statistics[0].passes.key")
        return Redaction(json.dumps(obj).encode(), paths, True)
    fake.add("/fixtures", {"ids": "1001"}, (200, match_detail()))
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(h, "redact", v1)
        mp.setattr(h, "POLICY_VERSION", "redact-v1")
        run(con, "af_results", lambda s: AF(cfg, con, f).call(s, "/fixtures", {"ids": "1001"}, "detail"))
    con.execute("UPDATE schema_event SET applied_at_ms = ? WHERE user_version = 6", (now_ms() + 3_600_000,))


def test_damage_detected_from_envelope(env, monkeypatch):
    v1_damage(env, monkeypatch)
    cfg, con, fake, f = env
    d = damaged(con)
    assert len(d) == 1 and d[0]["endpoint"] == "/fixtures" and d[0]["changed_values"] == 3
    assert d[0]["patterns"] == {"$.response[*].players[*].players[*].statistics[*].passes.key": 3}


def test_damage_detected_from_v05_scrub_rewrite(env, monkeypatch):
    """Rows 39-41 on the VM: rewritten by the v0.5 scrub before the personal-only trigger existed."""
    cfg, con, fake, f = env
    fake.add("/fixtures", {"ids": "1001"}, (200, match_detail()))
    run(con, "af_results", lambda s: AF(cfg, con, f).call(s, "/fixtures", {"ids": "1001"}, "detail"))
    raw_id, path, sha = con.execute("SELECT raw_id, archive_path, sha256 FROM raw_response").fetchone()
    sql = con.execute("SELECT sql FROM sqlite_master WHERE name='archive_rewrite_personal_only'").fetchone()[0]
    con.execute("DROP TRIGGER archive_rewrite_personal_only")
    con.execute("INSERT INTO archive_rewrite VALUES (?,?,?,?,?,?,?,?)",
                (raw_id, path, path + ".rewritten", sha, "b" * 64, "redact-v1",
                 json.dumps(["$.response[0].players[0].players[0].statistics[0].passes.key"]), now_ms()))
    con.execute(sql)
    assert [x["evidence"] for x in damaged(con)] == ["archive_rewrite"]


def test_repair_refetches_and_links(env, monkeypatch, capsys):
    v1_damage(env, monkeypatch)
    cfg, con, fake, f = env
    main = patched_main(monkeypatch, fake)
    cfgp = str(cfg.db_path.parent.parent / "config.toml")
    assert main(["--config", cfgp, "repair-damaged"]) == 0
    dry = json.loads(capsys.readouterr().out)
    assert dry["damaged_rows"] == 1 and dry["per_endpoint"]["api_football /fixtures"]["changed_values"] == 3
    assert main(["--config", cfgp, "repair-damaged", "--apply", "--expect", "2"]) == 1     # wrong count: refused
    capsys.readouterr()
    assert main(["--config", cfgp, "repair-damaged", "--apply", "--expect", "1"]) == 0
    res = json.loads(capsys.readouterr().out)
    assert res == {"damaged_rows": 1, "distinct_requests": 1, "linked": 1}
    assert damaged(con) == []
    new_raw = con.execute("SELECT max(raw_id) FROM raw_response").fetchone()[0]
    path = con.execute("SELECT archive_path FROM v_archive_location WHERE raw_id=?", (new_raw,)).fetchone()[0]
    body = json.loads(Archive.read(Path(path))["body"])
    assert [p["statistics"][0]["passes"]["key"] for p in body["response"][0]["players"][0]["players"]] == [2, 0, 5]


def test_export_skips_damaged_and_keeps_match_detail_intact(env, monkeypatch, tmp_path):
    v1_damage(env, monkeypatch)
    cfg, con, fake, f = env
    r = export_fixtures(con, tmp_path, (AF_KEY, OP_KEY), {})
    assert not [w for w in r["written"] if "fixtures_by_ids" in w]       # the damaged one is not exported
    assert "api_football /fixtures?ids" in r["missing_endpoints"]


def test_verify_detects_missing_file_and_leaked_key(env, monkeypatch):
    cfg, con, fake, f = env
    fake.add("/status", {}, (200, af_page({"note": f"echo {AF_KEY}"})))
    with pytest.MonkeyPatch.context() as mp:
        no_redaction(mp)
        run(con, "af_bootstrap", lambda s: AF(cfg, con, f).call(s, "/status", {}, "status"))
    r = verify(con, (AF_KEY,))
    assert r["problem_count"] == 1 and "live secret" in r["problems"][0][1]
    for p in cfg.archive_dir.rglob("*.json.gz"):
        p.unlink()
    assert verify(con, (AF_KEY,))["problems"][0][1] == "file missing"
