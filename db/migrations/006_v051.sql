-- =====================================================================
-- Migration 006 (schema v5 -> v6), v0.5.1 hotfix: redaction destroyed data.
-- =====================================================================

-- Endpoints that carry account data by design. Field-based redaction and
-- archive rewrites are allowed ONLY for these.
CREATE TABLE personal_endpoint (
  provider  TEXT NOT NULL,
  endpoint  TEXT NOT NULL,
  reason    TEXT NOT NULL,
  PRIMARY KEY (provider, endpoint)
) STRICT;
INSERT INTO personal_endpoint VALUES
  ('api_football', '/status', 'account first name, last name, email'),
  ('oddspapi', '/v4/account', 'api_key and email in the body');
CREATE TRIGGER personal_endpoint_no_update BEFORE UPDATE ON personal_endpoint BEGIN SELECT RAISE(ABORT, 'personal_endpoint changes need a migration'); END;
CREATE TRIGGER personal_endpoint_no_delete BEFORE DELETE ON personal_endpoint BEGIN SELECT RAISE(ABORT, 'personal_endpoint changes need a migration'); END;

-- From now on an archive rewrite is refused unless the record's endpoint is personal.
-- (Rows 39-41, rewritten by v0.5 before this existed, stay as evidence.)
CREATE TRIGGER archive_rewrite_personal_only
BEFORE INSERT ON archive_rewrite
BEGIN
  SELECT RAISE(ABORT, 'archive_rewrite: endpoint is not in personal_endpoint; data records are never rewritten')
   WHERE NOT EXISTS (SELECT 1 FROM raw_response r JOIN api_request a ON a.request_id = r.request_id
                       JOIN personal_endpoint p ON p.provider = a.provider AND p.endpoint = a.endpoint
                      WHERE r.raw_id = NEW.raw_id);
END;

-- Originals of a rewrite are moved aside, not deleted, until confirmed.
CREATE TABLE archive_superseded (
  raw_id            INTEGER PRIMARY KEY REFERENCES archive_rewrite(raw_id),
  superseded_path   TEXT    NOT NULL,
  moved_at_ms       INTEGER NOT NULL,
  verified_diff     TEXT    NOT NULL,        -- JSON: the only paths that differ, all personal fields
  deleted_at_ms     INTEGER,
  confirmed_by      TEXT,
  CHECK ((deleted_at_ms IS NULL) = (confirmed_by IS NULL))
) STRICT;
CREATE TRIGGER archive_superseded_update_only_to_delete
BEFORE UPDATE ON archive_superseded
WHEN NOT (OLD.deleted_at_ms IS NULL AND NEW.deleted_at_ms IS NOT NULL AND NEW.confirmed_by IS NOT NULL
          AND NEW.raw_id = OLD.raw_id AND NEW.superseded_path = OLD.superseded_path
          AND NEW.moved_at_ms = OLD.moved_at_ms AND NEW.verified_diff = OLD.verified_diff)
BEGIN SELECT RAISE(ABORT, 'archive_superseded: only recording the confirmed deletion is allowed'); END;
CREATE TRIGGER archive_superseded_no_delete BEFORE DELETE ON archive_superseded BEGIN SELECT RAISE(ABORT, 'archive_superseded is append-only'); END;

-- Which redaction rule fired on which response, and how often. A field rule
-- can only fire on a personal endpoint (enforced); anything firing on a data
-- endpoint (a live key echoed, or a detected personal field) is an alert.
CREATE TABLE raw_redaction (
  raw_id   INTEGER NOT NULL REFERENCES raw_response(raw_id),
  rule     TEXT    NOT NULL CHECK (rule LIKE 'field:%' OR rule LIKE 'value:%' OR rule LIKE 'bytes:%' OR rule LIKE 'detect:%'),
  n        INTEGER NOT NULL CHECK (n > 0),
  policy   TEXT    NOT NULL,
  PRIMARY KEY (raw_id, rule)
) STRICT;
CREATE TRIGGER raw_redaction_field_rules_personal_only
BEFORE INSERT ON raw_redaction
WHEN NEW.rule LIKE 'field:%'
BEGIN
  SELECT RAISE(ABORT, 'raw_redaction: a field rule fired on a data endpoint')
   WHERE NOT EXISTS (SELECT 1 FROM raw_response r JOIN api_request a ON a.request_id = r.request_id
                       JOIN personal_endpoint p ON p.provider = a.provider AND p.endpoint = a.endpoint
                      WHERE r.raw_id = NEW.raw_id);
END;
CREATE TRIGGER raw_redaction_no_update BEFORE UPDATE ON raw_redaction BEGIN SELECT RAISE(ABORT, 'raw_redaction is append-only'); END;
CREATE TRIGGER raw_redaction_no_delete BEFORE DELETE ON raw_redaction BEGIN SELECT RAISE(ABORT, 'raw_redaction is append-only'); END;

CREATE VIEW v_redaction_alerts AS
SELECT x.raw_id, a.provider, a.endpoint, x.rule, x.n, r.received_at_ms
  FROM raw_redaction x JOIN raw_response r ON r.raw_id = x.raw_id JOIN api_request a ON a.request_id = r.request_id
 WHERE NOT EXISTS (SELECT 1 FROM personal_endpoint p WHERE p.provider = a.provider AND p.endpoint = a.endpoint);

-- Responses damaged by redact-v1 and the re-fetch that replaces them.
CREATE TABLE raw_refetch (
  damaged_raw_id      INTEGER PRIMARY KEY REFERENCES raw_response(raw_id),
  refetch_request_id  INTEGER NOT NULL REFERENCES api_request(request_id),
  damage              TEXT    NOT NULL,      -- JSON: rule/paths summary from the damaged envelope
  created_at_ms       INTEGER NOT NULL
) STRICT;
CREATE TRIGGER raw_refetch_same_request
BEFORE INSERT ON raw_refetch
BEGIN
  SELECT RAISE(ABORT, 'raw_refetch: re-fetch must use the same provider, endpoint and parameters')
   WHERE NOT EXISTS (SELECT 1 FROM raw_response d JOIN api_request ad ON ad.request_id = d.request_id
                       JOIN api_request ar ON ar.request_id = NEW.refetch_request_id
                      WHERE d.raw_id = NEW.damaged_raw_id AND ar.provider = ad.provider
                        AND ar.endpoint = ad.endpoint AND ar.params_canonical = ad.params_canonical
                        AND ar.request_id <> ad.request_id);
END;
CREATE TRIGGER raw_refetch_no_update BEFORE UPDATE ON raw_refetch BEGIN SELECT RAISE(ABORT, 'raw_refetch is append-only'); END;
CREATE TRIGGER raw_refetch_no_delete BEFORE DELETE ON raw_refetch BEGIN SELECT RAISE(ABORT, 'raw_refetch is append-only'); END;

-- When each migration ran: bounds the redact-v1 era (envelopes written between
-- the v0.5 deploy and this migration are the only ones that can carry v1 damage).
CREATE TABLE schema_event (
  user_version   INTEGER PRIMARY KEY,
  applied_at_ms  INTEGER NOT NULL
) STRICT;
INSERT INTO schema_event VALUES (6, CAST(unixepoch('subsec') * 1000 AS INTEGER));

PRAGMA user_version = 6;
