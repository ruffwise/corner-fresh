"""Redaction of secrets and personal data from response bodies (policy redact-v2).

This is the ONE permitted change to a raw body. It runs before hashing,
archiving or parsing.

v2 exists because v1 destroyed real data: v1 applied field-name rules to every
endpoint, and the bare name "key" matched api-football's key-passes statistic
(passes.key) in match-detail responses. v2 separates the two jobs:

  PERSONAL endpoints (an explicit list: api-football /status, OddsPapi
  /v4/account) carry account data by design. On these only, field rules
  replace the values of personal and credential fields.

  DATA endpoints (everything else) are never changed by field rules. Only a
  live secret (one of the configured API keys) found in a string or in the raw
  bytes is replaced, because a key echoed into data is itself an incident.
  Personal-looking field names on a data endpoint are COUNTED (detection
  rules), never altered, and the watchdog alerts on them.

Every rule that fires is counted per response (raw_redaction), so the daily
report shows which rule touched which endpoint. If nothing fires, the
original bytes are returned unchanged.
"""
from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field

POLICY_VERSION = "redact-v2"
MARK = "[REDACTED]"

PERSONAL_ENDPOINTS = frozenset({("api_football", "/status"), ("oddspapi", "/v4/account")})

# Applied ONLY on personal endpoints, at any depth, case-insensitive exact names.
PERSONAL_FIELDS = frozenset({
    "api_key", "apikey", "api-key", "key", "token", "access_token", "refresh_token", "secret", "client_secret",
    "password", "email", "e-mail", "firstname", "lastname", "first_name", "last_name", "phone", "username",
})
ACCOUNT_PARENTS = frozenset({"account", "user", "owner", "customer"})
ACCOUNT_ONLY_FIELDS = frozenset({"name"})

# Counted (never altered) when they appear with a value on a DATA endpoint.
# Deliberately excludes names that are football data in these APIs (key, token, name, phone...).
DETECT_ON_DATA = frozenset({"api_key", "apikey", "api-key", "password", "email", "e-mail", "firstname", "lastname",
                            "access_token", "refresh_token", "client_secret"})


@dataclass
class Redaction:
    body: bytes
    paths: list[str] = field(default_factory=list)          # paths CHANGED
    reserialised: bool = False
    rules: Counter = field(default_factory=Counter)          # rule -> count, changed or detected


def is_personal(provider: str, endpoint: str) -> bool:
    return (provider, endpoint) in PERSONAL_ENDPOINTS


def _walk(obj, secrets, personal: bool, path: str, parent: str | None, paths: list[str], rules: Counter):
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            kl = str(k).lower()
            p = f"{path}.{k}"
            hit = personal and (kl in PERSONAL_FIELDS or (parent in ACCOUNT_PARENTS and kl in ACCOUNT_ONLY_FIELDS))
            if hit and v not in (None, ""):
                if v != MARK:
                    paths.append(p)
                    rules[f"field:{kl}"] += 1
                out[k] = MARK
                continue
            if not personal and kl in DETECT_ON_DATA and v not in (None, "", MARK):
                rules[f"detect:{kl}"] += 1          # counted for the alert; value left untouched
            out[k] = _walk(v, secrets, personal, p, kl, paths, rules)
        return out
    if isinstance(obj, list):
        return [_walk(v, secrets, personal, f"{path}[{i}]", parent, paths, rules) for i, v in enumerate(obj)]
    if isinstance(obj, str):
        s = obj
        for sec in secrets:
            if sec in s:
                s = s.replace(sec, MARK)
        if s != obj:
            paths.append(path)
            rules["value:live_secret"] += 1
        return s
    return obj


def redact(body: bytes, secrets: tuple[str, ...], provider: str, endpoint: str) -> Redaction:
    secrets = tuple(s for s in secrets if s and len(s) >= 6)
    personal = is_personal(provider, endpoint)
    try:
        parsed = json.loads(body) if body else None
    except ValueError:
        parsed = None
    if parsed is None:
        out, rules = body, Counter()
        for sec in secrets:
            b = sec.encode()
            if b in out:
                rules["bytes:live_secret"] += out.count(b)
                out = out.replace(b, MARK.encode())
        return Redaction(out, ["<bytes>"] if rules else [], False, rules)
    paths: list[str] = []
    rules: Counter = Counter()
    cleaned = _walk(parsed, secrets, personal, "$", None, paths, rules)
    if not paths:
        return Redaction(body, [], False, rules)            # exact original bytes (detections still counted)
    return Redaction(json.dumps(cleaned, ensure_ascii=False, separators=(",", ":")).encode(), paths, True, rules)


def redact_text(value: str, secrets: tuple[str, ...]) -> str:
    """Header values and other free text: live secrets only."""
    for sec in (s for s in secrets if s and len(s) >= 6):
        value = value.replace(sec, MARK)
    return value
