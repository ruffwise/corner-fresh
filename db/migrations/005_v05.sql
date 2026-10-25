-- =====================================================================
-- Migration 005 (schema v4 -> v5), design v0.5. Additive except v_close,
-- which is replaced by rule-versioned close tables.
-- =====================================================================

-- --- Fix 1: archive rewrites for redaction of already-archived bodies --------
-- The raw_response row is append-only and keeps its original path and hash;
-- this table records the one permitted rewrite (redaction) and where the
-- rewritten envelope now lives. Readers resolve paths through it.
CREATE TABLE archive_rewrite (
  raw_id            INTEGER PRIMARY KEY REFERENCES raw_response(raw_id),
  old_archive_path  TEXT    NOT NULL,
  new_archive_path  TEXT    NOT NULL,
  old_body_sha256   TEXT    NOT NULL CHECK (length(old_body_sha256) = 64),
  new_body_sha256   TEXT    NOT NULL CHECK (length(new_body_sha256) = 64),
  policy            TEXT    NOT NULL,
  redacted_paths    TEXT    NOT NULL,          -- JSON list of paths; never values
  rewritten_at_ms   INTEGER NOT NULL,
  CHECK (old_body_sha256 <> new_body_sha256),
  CHECK (old_archive_path <> new_archive_path)
) STRICT;
CREATE TRIGGER archive_rewrite_matches_raw
BEFORE INSERT ON archive_rewrite
BEGIN
  SELECT RAISE(ABORT, 'archive_rewrite: old path/hash differ from the raw_response row')
   WHERE NOT EXISTS (SELECT 1 FROM raw_response r WHERE r.raw_id = NEW.raw_id
                        AND r.archive_path = NEW.old_archive_path AND r.sha256 = NEW.old_body_sha256);
END;
CREATE TRIGGER archive_rewrite_no_update BEFORE UPDATE ON archive_rewrite BEGIN SELECT RAISE(ABORT, 'archive_rewrite is append-only'); END;
CREATE TRIGGER archive_rewrite_no_delete BEFORE DELETE ON archive_rewrite BEGIN SELECT RAISE(ABORT, 'archive_rewrite is append-only'); END;

CREATE VIEW v_archive_location AS
SELECT r.raw_id, coalesce(w.new_archive_path, r.archive_path) AS archive_path,
       coalesce(w.new_body_sha256, r.sha256) AS body_sha256, (w.raw_id IS NOT NULL) AS rewritten
  FROM raw_response r LEFT JOIN archive_rewrite w ON w.raw_id = r.raw_id;

-- --- Fix 3: bookmaker catalog; unnamed books can never be approved ------------
CREATE TABLE provider_book_catalog (
  provider            TEXT    NOT NULL REFERENCES provider(provider),
  provider_book_key   TEXT    NOT NULL,
  provider_book_name  TEXT,                     -- as received; NULL kept as a record of the feed
  raw_id              INTEGER NOT NULL REFERENCES raw_response(raw_id),
  PRIMARY KEY (provider, provider_book_key, raw_id)
) STRICT;
CREATE TRIGGER provider_bookmaker_map_requires_named_book
BEFORE INSERT ON provider_bookmaker_map
BEGIN
  SELECT RAISE(ABORT, 'provider_bookmaker_map: book absent from the provider catalog, or its latest catalog name is empty')
   WHERE coalesce(length(trim((SELECT c.provider_book_name FROM provider_book_catalog c
                                WHERE c.provider = NEW.provider AND c.provider_book_key = NEW.provider_book_key
                                ORDER BY c.raw_id DESC LIMIT 1))), 0) = 0;
END;
CREATE TRIGGER provider_book_catalog_no_update BEFORE UPDATE ON provider_book_catalog BEGIN SELECT RAISE(ABORT, 'provider_book_catalog is append-only'); END;
CREATE TRIGGER provider_book_catalog_no_delete BEFORE DELETE ON provider_book_catalog BEGIN SELECT RAISE(ABORT, 'provider_book_catalog is append-only'); END;

-- --- Fix 2: actual-start sources ------------------------------------------------
ALTER TABLE outcome_obs ADD COLUMN first_half_start_ms INTEGER;            -- api-football fixture.periods.first
ALTER TABLE provider_fixture_candidate ADD COLUMN true_start_ms INTEGER;   -- OddsPapi trueStartTime

-- --- Fix 2: rule-versioned close (replaces v_close) --------------------------------
DROP VIEW v_close;

CREATE TABLE close_rule (
  rule_version  TEXT    PRIMARY KEY,
  buffer_ms     INTEGER NOT NULL CHECK (buffer_ms >= 0),
  tolerance_ms  INTEGER NOT NULL CHECK (tolerance_ms > 0),
  description   TEXT    NOT NULL
) STRICT;
INSERT INTO close_rule VALUES ('close-v1', 120000, 600000,
  'cutoff = min(api-football scheduled kickoff, api-football first-half start, OddsPapi trueStartTime; ' ||
  'those present) - 2 min; no close if the present sources span > 10 min or the match is not FT/AET/PEN; ' ||
  'close = the record in force at the cutoff, which must be active; inactive or absent means no close.');
CREATE TRIGGER close_rule_no_update BEFORE UPDATE ON close_rule BEGIN SELECT RAISE(ABORT, 'close_rule is frozen; add a new rule_version'); END;
CREATE TRIGGER close_rule_no_delete BEFORE DELETE ON close_rule BEGIN SELECT RAISE(ABORT, 'close_rule is frozen'); END;

CREATE TABLE close_cutoff (
  fixture_id          INTEGER NOT NULL REFERENCES fixture(fixture_id),
  rule_version        TEXT    NOT NULL REFERENCES close_rule(rule_version),
  scheduled_ko_ms     INTEGER NOT NULL,
  af_actual_start_ms  INTEGER,
  op_true_start_ms    INTEGER,
  status_short        TEXT    NOT NULL,
  cutoff_ms           INTEGER,
  excluded_reason     TEXT    CHECK (excluded_reason IS NULL OR excluded_reason IN (
                        'status_not_finished', 'start_sources_disagree')),
  basis               TEXT    NOT NULL,
  computed_at_ms      INTEGER NOT NULL,
  PRIMARY KEY (fixture_id, rule_version),
  CHECK ((cutoff_ms IS NULL) = (excluded_reason IS NOT NULL))
) STRICT;

CREATE TRIGGER close_cutoff_rule
BEFORE INSERT ON close_cutoff
BEGIN
  SELECT RAISE(ABORT, 'close_cutoff: scheduled_ko_ms/status_short are not the latest api-football values')
   WHERE NOT EXISTS (SELECT 1 FROM v_fixture_latest l WHERE l.fixture_id = NEW.fixture_id
                        AND l.kickoff_ms = NEW.scheduled_ko_ms AND l.status_short = NEW.status_short);
  SELECT RAISE(ABORT, 'close_cutoff: a finished-status exclusion must not be claimed on a finished match')
   WHERE NEW.excluded_reason = 'status_not_finished' AND NEW.status_short IN ('FT', 'AET', 'PEN');
  SELECT RAISE(ABORT, 'close_cutoff: unfinished match must be excluded')
   WHERE NEW.cutoff_ms IS NOT NULL AND NEW.status_short NOT IN ('FT', 'AET', 'PEN');
  SELECT RAISE(ABORT, 'close_cutoff: start sources span more than the tolerance; must be excluded')
   WHERE NEW.cutoff_ms IS NOT NULL
     AND max(NEW.scheduled_ko_ms, coalesce(NEW.af_actual_start_ms, NEW.scheduled_ko_ms), coalesce(NEW.op_true_start_ms, NEW.scheduled_ko_ms))
       - min(NEW.scheduled_ko_ms, coalesce(NEW.af_actual_start_ms, NEW.scheduled_ko_ms), coalesce(NEW.op_true_start_ms, NEW.scheduled_ko_ms))
       > (SELECT tolerance_ms FROM close_rule WHERE rule_version = NEW.rule_version);
  SELECT RAISE(ABORT, 'close_cutoff: disagreement exclusion claimed but sources agree')
   WHERE NEW.excluded_reason = 'start_sources_disagree'
     AND max(NEW.scheduled_ko_ms, coalesce(NEW.af_actual_start_ms, NEW.scheduled_ko_ms), coalesce(NEW.op_true_start_ms, NEW.scheduled_ko_ms))
       - min(NEW.scheduled_ko_ms, coalesce(NEW.af_actual_start_ms, NEW.scheduled_ko_ms), coalesce(NEW.op_true_start_ms, NEW.scheduled_ko_ms))
       <= (SELECT tolerance_ms FROM close_rule WHERE rule_version = NEW.rule_version);
  SELECT RAISE(ABORT, 'close_cutoff: cutoff_ms must equal min(present start sources) - buffer')
   WHERE NEW.cutoff_ms IS NOT NULL
     AND NEW.cutoff_ms <> min(NEW.scheduled_ko_ms, coalesce(NEW.af_actual_start_ms, NEW.scheduled_ko_ms),
                             coalesce(NEW.op_true_start_ms, NEW.scheduled_ko_ms))
                         - (SELECT buffer_ms FROM close_rule WHERE rule_version = NEW.rule_version);
  SELECT RAISE(ABORT, 'close_cutoff: af_actual_start_ms differs from the latest api-football outcome')
   WHERE NEW.af_actual_start_ms IS NOT (SELECT o.first_half_start_ms FROM outcome_obs o
                                         WHERE o.fixture_id = NEW.fixture_id AND o.source = 'api_football'
                                         ORDER BY o.recorded_at_ms DESC, o.outcome_id DESC LIMIT 1);
END;

CREATE TABLE close_price (
  fixture_id           INTEGER NOT NULL,
  rule_version         TEXT    NOT NULL,
  bookmaker_id         INTEGER NOT NULL REFERENCES bookmaker(bookmaker_id),
  market_key           TEXT    NOT NULL REFERENCES market_def(market_key),
  line_q               INTEGER NOT NULL,
  selection            TEXT    NOT NULL,
  hist_id              INTEGER NOT NULL REFERENCES price_history(hist_id),
  close_price          REAL    NOT NULL,
  close_record_ms      INTEGER NOT NULL,
  last_change_ms       INTEGER NOT NULL,
  book_last_record_ms  INTEGER NOT NULL,
  computed_at_ms       INTEGER NOT NULL,
  PRIMARY KEY (fixture_id, rule_version, bookmaker_id, market_key, line_q, selection),
  FOREIGN KEY (fixture_id, rule_version) REFERENCES close_cutoff(fixture_id, rule_version),
  CHECK (last_change_ms <= close_record_ms),
  CHECK (close_record_ms <= book_last_record_ms)
) STRICT;

-- The schema itself guarantees that no record at or after the cutoff can be a close.
CREATE TRIGGER close_price_rule
BEFORE INSERT ON close_price
BEGIN
  SELECT RAISE(ABORT, 'close_price: fixture is excluded under this rule (no cutoff)')
   WHERE (SELECT cutoff_ms FROM close_cutoff WHERE fixture_id = NEW.fixture_id AND rule_version = NEW.rule_version) IS NULL;
  SELECT RAISE(ABORT, 'close_price: record is not this fixture/book/market/line/selection, or not active, or price differs')
   WHERE NOT EXISTS (SELECT 1 FROM price_history h WHERE h.hist_id = NEW.hist_id
                        AND h.fixture_id = NEW.fixture_id AND h.bookmaker_id = NEW.bookmaker_id
                        AND h.market_key = NEW.market_key AND h.line_q = NEW.line_q AND h.selection = NEW.selection
                        AND h.active = 1 AND h.price = NEW.close_price AND h.valid_from_ms = NEW.close_record_ms);
  SELECT RAISE(ABORT, 'close_price: record is at or after the cutoff (look-ahead)')
   WHERE NEW.close_record_ms >= (SELECT cutoff_ms FROM close_cutoff WHERE fixture_id = NEW.fixture_id AND rule_version = NEW.rule_version);
  SELECT RAISE(ABORT, 'close_price: a later pre-cutoff record exists, so this record was not in force at the cutoff')
   WHERE EXISTS (SELECT 1 FROM price_history h2 JOIN price_history h ON h.hist_id = NEW.hist_id
                  WHERE h2.parse_run_id = h.parse_run_id AND h2.fixture_id = h.fixture_id
                    AND h2.bookmaker_id = h.bookmaker_id AND h2.market_key = h.market_key
                    AND h2.line_q = h.line_q AND h2.selection = h.selection
                    AND h2.valid_from_ms > h.valid_from_ms
                    AND h2.valid_from_ms < (SELECT cutoff_ms FROM close_cutoff WHERE fixture_id = NEW.fixture_id
                                                                                  AND rule_version = NEW.rule_version));
  SELECT RAISE(ABORT, 'close_price: book_last_record_ms is not the last pre-cutoff record of this book and fixture')
   WHERE NEW.book_last_record_ms IS NOT (
     SELECT max(h2.valid_from_ms) FROM price_history h2 JOIN price_history h ON h.hist_id = NEW.hist_id
      WHERE h2.parse_run_id = h.parse_run_id AND h2.fixture_id = h.fixture_id AND h2.bookmaker_id = h.bookmaker_id
        AND h2.valid_from_ms < (SELECT cutoff_ms FROM close_cutoff WHERE fixture_id = NEW.fixture_id
                                                                      AND rule_version = NEW.rule_version));
END;

CREATE TRIGGER close_cutoff_no_update BEFORE UPDATE ON close_cutoff BEGIN SELECT RAISE(ABORT, 'close_cutoff is append-only'); END;
CREATE TRIGGER close_cutoff_no_delete BEFORE DELETE ON close_cutoff BEGIN SELECT RAISE(ABORT, 'close_cutoff is append-only'); END;
CREATE TRIGGER close_price_no_update BEFORE UPDATE ON close_price BEGIN SELECT RAISE(ABORT, 'close_price is append-only'); END;
CREATE TRIGGER close_price_no_delete BEFORE DELETE ON close_price BEGIN SELECT RAISE(ABORT, 'close_price is append-only'); END;

PRAGMA user_version = 5;
