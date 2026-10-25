"""Tests built from REAL payloads captured in the production archive (fix 4).

Payloads live in tests/fixtures/real/<provider>/<endpoint>_<raw_id>.json.gz,
exported (and re-redacted) by `collector export-fixtures`. Each is replayed
through the same code path production uses, via the fake transport.

test_every_endpoint_has_a_real_payload FAILS until every required endpoint
has at least one payload: an absent fixture must not pass silently.
"""
from __future__ import annotations

import gzip
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from collector import diag  # noqa: E402
from collector.af import AF  # noqa: E402
from collector.archive_tools import REQUIRED_ENDPOINTS, export_fixtures, slug  # noqa: E402
from collector.op import OP  # noqa: E402
from collector.redact import MARK, redact  # noqa: E402
from test_collector import AF_KEY, OP_KEY, af_page, env, run  # noqa: E402,F401

REAL = ROOT / "tests" / "fixtures" / "real"
ACCOUNT_ENDPOINTS = {("api_football", "/status"), ("oddspapi", "/v4/account")}


def load_all():
    out = []
    if REAL.exists():
        for p in sorted(REAL.rglob("*.json.gz")):
            doc = json.loads(gzip.decompress(p.read_bytes()))
            out.append((p, doc["meta"], doc["body"]))
    return out


PAYLOADS = load_all()


def of(provider, endpoint):
    return [(p, m, b) for p, m, b in PAYLOADS if m["provider"] == provider and m["endpoint"] == endpoint]


def key_of(m):
    if m["endpoint"] == "/fixtures" and "ids" in m.get("params", {}):
        return (m["provider"], "/fixtures?ids")
    return (m["provider"], m["endpoint"])


def test_every_endpoint_has_a_real_payload():
    have = {key_of(m) for _, m, _ in PAYLOADS}
    missing = [f"{p} {e}" for p, e in REQUIRED_ENDPOINTS if (p, e) not in have]
    assert not missing, ("no real payload committed for: " + ", ".join(missing) +
                         ". Run `run.sh export-fixtures` on the VM (RUNBOOK §R6) and commit the files.")


@pytest.mark.parametrize("item", PAYLOADS, ids=lambda x: x[0].name)
def test_redaction_leaves_real_data_byte_identical(item):
    """The v0.5 failure: a field rule ("key") matched passes.key in match detail.
    Every real data payload must pass through redaction byte-identical."""
    path, meta, body = item
    if (meta["provider"], meta["endpoint"]) in ACCOUNT_ENDPOINTS:
        return
    raw = json.dumps(body).encode()
    r = redact(raw, (AF_KEY, OP_KEY), meta["provider"], meta["endpoint"])
    assert r.body is raw and r.paths == [], f"redaction would change {r.paths[:5]} in {path.name}"
    assert not [k for k in r.rules if k.startswith(("field:", "value:", "bytes:"))]


@pytest.mark.parametrize("item", [x for x in PAYLOADS if key_of(x[1]) == ("api_football", "/fixtures?ids")],
                         ids=lambda x: x[0].name)
def test_real_match_detail_keeps_key_passes(item):
    _, meta, body = item
    keys = [p["statistics"][0]["passes"].get("key") for it in body["response"] for t in it.get("players", [])
            for p in t.get("players", []) if p.get("statistics")]
    assert keys, "match detail without player statistics: export a finished match"
    assert MARK not in keys and any(isinstance(k, int) for k in keys)


@pytest.mark.parametrize("item", of("api_football", "/status") + of("oddspapi", "/v4/account"),
                         ids=lambda x: x[0].name)
def test_account_payloads_carry_no_personal_values(item):
    _, _, body = item
    text = json.dumps(body)
    for k in ("email", "firstname", "lastname", "api_key", "apiKey"):
        if f'"{k}"' in text:
            assert f'"{k}": "{MARK}"' in text or f'"{k}": null' in text, k


@pytest.mark.parametrize("item", of("api_football", "/odds/bookmakers"), ids=lambda x: x[0].name)
def test_real_bookmaker_list_parses(item, env):
    _, meta, body = item
    cfg, con, fake, f = env
    fake.add("/status", {}, (200, af_page({"subscription": {}})))
    fake.add("/odds/bookmakers", meta["params"], (200, body))
    fake.add("/odds/bets", {}, (200, af_page([])))
    rep = run(con, "af_bootstrap", lambda s: AF(cfg, con, f).bootstrap(s))
    assert diag.bookmaker_check(rep["bookmakers"]).startswith(("PASS", "REVIEW", "FAIL"))
    n = len(body["response"])
    assert con.execute("SELECT count(*) FROM provider_book_catalog").fetchone()[0] == \
        len({str(b["id"]) for b in body["response"] if b.get("id") is not None}) and n > 0


@pytest.mark.parametrize("item", of("api_football", "/fixtures"), ids=lambda x: x[0].name)
def test_real_fixture_list_parses(item, env):
    _, meta, body = item
    cfg, con, fake, f = env
    items = body["response"]
    leagues = {(it["league"]["id"], it["league"]["season"]) for it in items}
    for lid, season in leagues:
        con.execute("INSERT OR IGNORE INTO league VALUES (?,?,?,?,?)", (lid, season, "x", "y", "League"))
    fake.add("/fixtures", meta["params"], (200, body))
    res = run(con, "af_fixtures", lambda s: AF(cfg, con, f).call(s, "/fixtures", meta["params"], "replay"))
    for r in res:
        AF(cfg, con, f).record_fixtures(r)
    assert con.execute("SELECT count(*) FROM fixture").fetchone()[0] == len({it["fixture"]["id"] for it in items})
    assert con.execute("SELECT count(*) FROM v_fixture_latest WHERE kickoff_ms IS NULL").fetchone()[0] == 0


@pytest.mark.parametrize("item", of("api_football", "/fixtures/statistics"), ids=lambda x: x[0].name)
def test_real_statistics_corner_label_present(item):
    _, _, body = item
    types = {s.get("type") for t in body["response"] for s in t.get("statistics", [])}
    if body["response"]:
        assert "Corner Kicks" in types, f"exact label missing; labels seen: {sorted(map(str, types))[:20]}"


@pytest.mark.parametrize("item", of("api_football", "/odds"), ids=lambda x: x[0].name)
def test_real_odds_page_is_consistent(item):
    _, meta, body = item
    pg = body["paging"]
    assert 1 <= pg["current"] <= pg["total"]
    assert body["results"] == len(body["response"]) <= 10
    for fx in body["response"]:
        for b in fx.get("bookmakers", []):
            assert isinstance(b.get("id"), int)
            for bet in b.get("bets", []):
                assert {"id", "name", "values"} <= set(bet)


@pytest.mark.parametrize("item", of("oddspapi", "/v4/account"), ids=lambda x: x[0].name)
def test_real_account_quota_fields(item, env):
    _, _, body = item
    cfg, con, fake, f = env
    fake.add("/v4/account", {}, (200, body))
    acct = run(con, "op_account", lambda s: OP(cfg, con, f).account(s))
    assert isinstance(acct["request_limit"], int) and isinstance(acct["request_count"], int)


@pytest.mark.parametrize("item", of("oddspapi", "/v4/fixtures"), ids=lambda x: x[0].name)
def test_real_oddspapi_listing_drops_nothing(item, env):
    _, meta, body = item
    cfg, con, fake, f = env
    items = body if isinstance(body, list) else body.get("data", [])
    fake.add("/v4/account", {}, (200, {"request_limit": 250, "request_count": 0}))
    fake.add("/v4/fixtures", meta["params"], (200, body))
    tid = int(meta["params"]["tournamentId"])
    run(con, "op_fixtures", lambda s: OP(cfg, con, f).fixtures(s, force=[tid]))
    got = con.execute("SELECT count(DISTINCT provider_fixture_id) FROM provider_fixture_candidate").fetchone()[0]
    assert got == len({str(it["fixtureId"]) for it in items}), "listing rows silently dropped (shape drift)"


def test_export_round_trip_redacts(env, tmp_path):
    cfg, con, fake, f = env
    fake.add("/v4/account", {}, (200, {"api_key": OP_KEY, "email": "a@b.co", "request_limit": 250, "request_count": 1}))
    run(con, "op_account", lambda s: OP(cfg, con, f).account(s))
    r = export_fixtures(con, tmp_path, (AF_KEY, OP_KEY), {})
    assert any(slug("/v4/account") in w for w in r["written"])
    blob = b"".join(gzip.decompress(Path(w).read_bytes()) for w in r["written"])
    assert OP_KEY.encode() not in blob and b"a@b.co" not in blob
    assert "api_football /odds" in r["missing_endpoints"]          # absent endpoints are reported
