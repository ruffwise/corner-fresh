"""Fix 1: secrets and personal data never reach the archive, the off-box copy, or stdout.

Every test feeds a body that contains the secret and asserts on bytes on disk
and on captured stdout, not on intermediate objects.
"""
from __future__ import annotations

import email.utils
import gzip
import hashlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from collector import ledger  # noqa: E402
from collector.archive_tools import scrub, verify  # noqa: E402
from collector.redact import MARK, redact  # noqa: E402
from collector.store import Archive  # noqa: E402
from test_collector import AF_KEY, OP_KEY, af_page, env, patched_main, run  # noqa: E402,F401
from collector.af import AF  # noqa: E402

EMAIL = "someone@example.com"


def archive_bytes(cfg) -> bytes:
    return b"".join(gzip.decompress(p.read_bytes()) for p in cfg.archive_dir.rglob("*.json.gz"))


def test_op_account_key_and_email_never_stored_or_printed(env, monkeypatch, capsys):
    cfg, con, fake, f = env
    body = {"api_key": OP_KEY, "email": EMAIL, "request_limit": 250, "request_count": 7, "plan": "free",
            "created": "2026-10-01"}
    fake.add("/v4/account", {}, (200, body))
    main = patched_main(monkeypatch, fake)
    rc = main(["--config", str(cfg.db_path.parent.parent / "config.toml"), "op-account"])
    printed = capsys.readouterr()
    blob = archive_bytes(cfg)
    assert rc == 0
    for secret in (OP_KEY, EMAIL):
        assert secret.encode() not in blob, secret
        assert secret not in printed.out and secret not in printed.err, secret
    assert con.execute("SELECT request_limit, request_count FROM quota_obs").fetchone() == (250, 7)
    env_ = Archive.read(next(cfg.archive_dir.rglob("*.json.gz")))
    archived = json.loads(env_["body"])
    assert archived == {**body, "api_key": MARK, "email": MARK}           # the rest is unchanged
    assert sorted(env_["redaction"]["paths"]) == ["$.api_key", "$.email"]
    assert env_["body_sha256"] == hashlib.sha256(env_["body"].encode()).hexdigest()


def test_af_status_personal_fields_redacted(env):
    cfg, con, fake, f = env
    status = {"account": {"firstname": "Ada", "lastname": "Obi", "email": EMAIL},
              "subscription": {"plan": "Pro", "end": "2026-11-25", "active": True},
              "requests": {"current": 12, "limit_day": 7500}}
    fake.add("/status", {}, (200, af_page(status)))
    res = run(con, "af_bootstrap", lambda s: AF(cfg, con, f).call(s, "/status", {}, "status"))
    blob = archive_bytes(cfg)
    for v in ("Ada", "Obi", EMAIL):
        assert v.encode() not in blob
    assert res[0].body["response"]["subscription"]["plan"] == "Pro"     # parsers see the redacted body
    assert res[0].body["response"]["account"]["email"] == MARK


def test_secret_echoed_in_unexpected_field_is_scrubbed(env):
    cfg, con, fake, f = env
    fake.add("/status", {}, (200, af_page([], errors={"token": f"Invalid key {AF_KEY} supplied"})))
    with pytest.raises(ledger.SlotIncomplete):
        run(con, "af_bootstrap", lambda s: AF(cfg, con, f).call(s, "/status", {}, "status"))
    assert AF_KEY.encode() not in archive_bytes(cfg)


def test_secret_in_non_json_error_body_is_scrubbed(env):
    cfg, con, fake, f = env

    def plain(url, headers, timeout):
        return 401, {"date": "Sun, 25 Oct 2026 05:00:00 GMT"}, f"bad key {AF_KEY}".encode()
    f.transport = plain
    with pytest.raises(ledger.SlotIncomplete):
        run(con, "af_bootstrap", lambda s: AF(cfg, con, f).call(s, "/status", {}, "status"))
    assert AF_KEY.encode() not in archive_bytes(cfg)


def test_untouched_body_keeps_exact_bytes():
    raw = b'{"response": [ {"id": 4, "name": "Pinnacle", "keyPasses": 3} ],  "paging":{"current":1}}'
    r = redact(raw, (AF_KEY, OP_KEY))
    assert r.body is raw and r.paths == [] and not r.reserialised


def test_data_fields_that_look_similar_are_not_redacted():
    body = {"teams": {"home": {"name": "Arsenal"}}, "keyPasses": 4, "bookmakers": [{"id": 4, "name": "Pinnacle"}],
            "markets": {"101": {"outcomes": {}}}}
    r = redact(json.dumps(body).encode(), (AF_KEY,))
    assert r.paths == []


def test_response_header_echoing_key_is_scrubbed(env):
    cfg, con, fake, f = env

    def hdr(url, headers, timeout):
        return 200, {"date": email.utils.formatdate(usegmt=True), "x-debug": f"key={AF_KEY}"}, \
            json.dumps(af_page({"ok": 1})).encode()
    f.transport = hdr
    run(con, "af_bootstrap", lambda s: AF(cfg, con, f).call(s, "/status", {}, "status"))
    assert AF_KEY.encode() not in archive_bytes(cfg)


def no_redaction(monkeypatch):
    """Reproduce v0.4 behaviour (no redaction at all) to create a pre-fix archive."""
    import collector.http as h
    from collector.redact import Redaction
    monkeypatch.setattr(h, "redact", lambda body, secrets: Redaction(body))


def test_scrub_rewrites_pre_fix_archive_and_verify_passes(env, monkeypatch):
    """Simulates the VM: /status bodies archived by v0.4 code, before redaction existed."""
    cfg, con, fake, f = env
    no_redaction(monkeypatch)
    status = {"account": {"firstname": "Ada", "email": EMAIL}, "subscription": {"plan": "Pro"}}
    fake.add("/status", {}, (200, af_page(status)))
    run(con, "af_bootstrap", lambda s: AF(cfg, con, f).call(s, "/status", {}, "status"))
    assert EMAIL.encode() in archive_bytes(cfg)
    monkeypatch.undo()
    dry = scrub(con, Archive(cfg.archive_dir), (AF_KEY, OP_KEY), apply=False)
    assert dry["needing_redaction"] == 1 and dry["rewritten"] == 0 and EMAIL.encode() in archive_bytes(cfg)
    done = scrub(con, Archive(cfg.archive_dir), (AF_KEY, OP_KEY), apply=True)
    assert done["rewritten"] == 1
    assert EMAIL.encode() not in archive_bytes(cfg) and b"Ada" not in archive_bytes(cfg)
    assert verify(con, (AF_KEY, OP_KEY))["problem_count"] == 0
    assert scrub(con, Archive(cfg.archive_dir), (AF_KEY, OP_KEY), apply=True)["needing_redaction"] == 0
    loc = con.execute("SELECT rewritten FROM v_archive_location").fetchone()
    assert loc == (1,)


def test_verify_detects_missing_file_and_leaked_key(env, monkeypatch):
    cfg, con, fake, f = env
    no_redaction(monkeypatch)
    fake.add("/status", {}, (200, af_page({"note": f"echo {AF_KEY}"})))
    run(con, "af_bootstrap", lambda s: AF(cfg, con, f).call(s, "/status", {}, "status"))
    r = verify(con, (AF_KEY,))
    assert r["problem_count"] == 1 and "live secret" in r["problems"][0][1]
    for p in cfg.archive_dir.rglob("*.json.gz"):
        p.unlink()
    assert verify(con, (AF_KEY,))["problems"][0][1] == "file missing"
