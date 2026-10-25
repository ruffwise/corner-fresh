"""Redaction of secrets and personal data from response bodies.

This is the ONE permitted change to a raw body (DESIGN v0.5 §4.1). It runs
before hashing and archiving, so nothing downstream (archive, off-box copy,
parsers, stdout, journald) ever sees the original values.

Three layers, applied in order:
  1. Field rules: any JSON key whose lower-cased name is in SENSITIVE_KEYS,
     at any depth, in any endpoint's body, has its value replaced.
  2. Value scrub: any string anywhere in the JSON that CONTAINS one of the
     live secrets (the configured API keys) has the secret replaced, so a key
     echoed in an unexpected field (an error message, say) is still removed.
  3. Byte scrub: for bodies that are not JSON, every occurrence of a live
     secret in the raw bytes is replaced.
If nothing is redacted, the original bytes are returned unchanged.
If something is redacted, the JSON is re-serialised, and the envelope
records the paths (never the values) that were redacted.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

POLICY_VERSION = "redact-v1"
MARK = "[REDACTED]"

# Field names are matched case-insensitively and exactly (no substring match),
# so a market called "keyPasses" is untouched while "apiKey" is removed.
SENSITIVE_KEYS = frozenset({
    # credentials
    "api_key", "apikey", "api-key", "key", "token", "access_token", "secret", "password",
    # personal data returned by account endpoints
    "email", "e-mail", "firstname", "lastname", "first_name", "last_name", "name_full",
    "phone", "username",
})
# Keys that are personal only inside an account object; elsewhere they are data
# (a team or bookmaker "name"). Applied only below these parent keys.
ACCOUNT_PARENTS = frozenset({"account", "user", "owner", "customer"})
ACCOUNT_ONLY_KEYS = frozenset({"name"})


@dataclass
class Redaction:
    body: bytes
    paths: list[str] = field(default_factory=list)
    reserialised: bool = False


def _walk(obj, secrets: tuple[str, ...], path: str, parent: str | None, paths: list[str]):
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            kl = str(k).lower()
            p = f"{path}.{k}"
            if kl in SENSITIVE_KEYS or (parent in ACCOUNT_PARENTS and kl in ACCOUNT_ONLY_KEYS):
                if v not in (None, "", MARK):
                    paths.append(p)
                out[k] = MARK if v not in (None, "") else v
            else:
                out[k] = _walk(v, secrets, p, kl, paths)
        return out
    if isinstance(obj, list):
        return [_walk(v, secrets, f"{path}[{i}]", parent, paths) for i, v in enumerate(obj)]
    if isinstance(obj, str):
        s = obj
        for sec in secrets:
            if sec and sec in s:
                s = s.replace(sec, MARK)
        if s != obj:
            paths.append(path)
        return s
    return obj


def redact(body: bytes, secrets: tuple[str, ...]) -> Redaction:
    secrets = tuple(s for s in secrets if s and len(s) >= 6)
    try:
        parsed = json.loads(body) if body else None
    except ValueError:
        parsed = None
    if parsed is None:
        out = body
        hit = False
        for sec in secrets:
            b = sec.encode()
            if b in out:
                out = out.replace(b, MARK.encode())
                hit = True
        return Redaction(out, ["<bytes>"] if hit else [], False)
    paths: list[str] = []
    cleaned = _walk(parsed, secrets, "$", None, paths)
    if not paths:
        return Redaction(body)
    return Redaction(json.dumps(cleaned, ensure_ascii=False, separators=(",", ":")).encode(), paths, True)
