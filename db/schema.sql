-- =====================================================================
-- Pinnacle-close benchmark study — SQLite schema v0.4 (SQLite >= 3.42, STRICT)
--
-- Scope change from v0.3: no venue capture. Sources are api-football
-- (fixtures, statistics, outcomes, its odds) and OddsPapi (Pinnacle price
-- history). SportyBet appears only as bet-slip records for bets actually placed.
--
-- Layering:
--   RAW layer   (api_request, raw_response) RECORDS; never refuses on content.
--   PARSE layer (everything else) JUDGES; rebuildable from the raw archive.
-- Conventions: timestamps INTEGER ms since epoch UTC (_ms); lines INTEGER
-- quarter-units line_q = round(line*4); no-line markets use 0.
-- connect() must enable and verify PRAGMA foreign_keys (db/connect.py).
-- =====================================================================

-- ---------------------------------------------------------------------
-- Pre-registration
-- ---------------------------------------------------------------------
CREATE TABLE preregistration (
  prereg_id          INTEGER PRIMARY KEY,
  title              TEXT    NOT NULL,
  git_commit         TEXT    NOT NULL CHECK (length(git_commit) = 40),
  signed_tag         TEXT    NOT NULL UNIQUE,
  doc_sha256         TEXT    NOT NULL UNIQUE CHECK (length(doc_sha256) = 64),
  ots_proof_path     TEXT,
  committed_at_ms    INTEGER NOT NULL,
  effective_from_ms  INTEGER NOT NULL,
  CHECK (effective_from_ms >= committed_at_ms)
) STRICT;

-- ---------------------------------------------------------------------
-- Providers, endpoints, books, markets, allowlists
-- ---------------------------------------------------------------------
CREATE TABLE provider (
  provider     TEXT PRIMARY KEY,           -- 'api_football', 'oddspapi'
  paged        INTEGER NOT NULL CHECK (paged IN (0, 1)),
  description  TEXT NOT NULL
) STRICT;

CREATE TABLE endpoint_allowlist (
  provider  TEXT NOT NULL REFERENCES provider(provider),
  endpoint  TEXT NOT NULL,
  billable  INTEGER NOT NULL CHECK (billable IN (0, 1)),   -- counts against provider quota
  PRIMARY KEY (provider, endpoint)
) STRICT;

CREATE TABLE league (
  league_id  INTEGER NOT NULL,             -- api-football id
  season     INTEGER NOT NULL,
  name       TEXT    NOT NULL,
  country    TEXT    NOT NULL,
  league_type TEXT   NOT NULL CHECK (league_type IN ('League', 'Cup')),
  PRIMARY KEY (league_id, season)
) STRICT;

CREATE TABLE team (team_id INTEGER PRIMARY KEY, name TEXT NOT NULL) STRICT;   -- api-football ids

CREATE TABLE bookmaker (
  bookmaker_id  INTEGER PRIMARY KEY,
  name          TEXT NOT NULL UNIQUE,
  role          TEXT NOT NULL CHECK (role IN ('reference_primary', 'reference_member', 'observed'))
) STRICT;

CREATE TABLE provider_bookmaker_map (
  provider           TEXT    NOT NULL REFERENCES provider(provider),
  provider_book_key  TEXT    NOT NULL,
  bookmaker_id       INTEGER NOT NULL REFERENCES bookmaker(bookmaker_id),
  approved_in_commit TEXT    NOT NULL CHECK (length(approved_in_commit) = 40),
  PRIMARY KEY (provider, provider_book_key),
  UNIQUE (provider, bookmaker_id)
) STRICT;

CREATE TABLE market_def (
  market_key TEXT PRIMARY KEY,
  family     TEXT NOT NULL CHECK (family IN (
               'match_result', 'goals_total', 'asian_handicap', 'btts', 'team_goals',
               'double_chance', 'draw_no_bet', 'corners', 'cards', 'other')),
  scope      TEXT NOT NULL CHECK (scope IN ('match', 'home', 'away')),
  period     TEXT NOT NULL CHECK (period IN ('FT', '1H', '2H')),
  structure  TEXT NOT NULL CHECK (structure IN (
               'three_way', 'ou_half', 'ou_whole', 'ou_quarter', 'yes_no',
               'handicap_half', 'handicap_whole', 'handicap_quarter',
               'two_way_other', 'exact', 'odd_even', 'race', 'first_last'))
) STRICT;

-- Exact-match allowlists keyed by provider. api-football labels are
-- '<bet_id>|<bet_name>'; OddsPapi labels are the numeric market id as text,
-- and value labels the numeric outcome id as text.
CREATE TABLE provider_market_allowlist (
  provider              TEXT NOT NULL REFERENCES provider(provider),
  provider_market_label TEXT NOT NULL,
  market_key            TEXT NOT NULL REFERENCES market_def(market_key),
  approved_in_commit    TEXT NOT NULL CHECK (length(approved_in_commit) = 40),
  PRIMARY KEY (provider, provider_market_label),
  UNIQUE (provider, provider_market_label, market_key)
) STRICT;

CREATE TABLE value_allowlist (
  provider           TEXT    NOT NULL REFERENCES provider(provider),
  market_key         TEXT    NOT NULL REFERENCES market_def(market_key),
  value_label        TEXT    NOT NULL,
  selection          TEXT    NOT NULL CHECK (selection IN (
                       'home', 'draw', 'away', 'over', 'under', 'yes', 'no',
                       'home_or_draw', 'home_or_away', 'draw_or_away', 'odd', 'even', 'exact', 'none')),
  line_q             INTEGER NOT NULL,
  approved_in_commit TEXT    NOT NULL CHECK (length(approved_in_commit) = 40),
  PRIMARY KEY (provider, market_key, value_label),
  UNIQUE (provider, market_key, value_label, selection, line_q)
) STRICT;

-- ---------------------------------------------------------------------
-- Expected-slots ledger
-- ---------------------------------------------------------------------
CREATE TABLE capture_slot (
  slot_id            INTEGER PRIMARY KEY,
  job                TEXT    NOT NULL CHECK (job IN (
                       'af_bootstrap', 'af_fixtures', 'af_odds', 'af_results',
                       'op_account', 'op_catalog', 'op_fixtures', 'op_history', 'op_backfill')),
  scheduled_for_ms   INTEGER NOT NULL,
  status             TEXT    NOT NULL DEFAULT 'pending'
                       CHECK (status IN ('pending', 'running', 'complete', 'failed', 'missed')),
  started_at_ms      INTEGER,
  completed_at_ms    INTEGER,
  expected_requests  INTEGER CHECK (expected_requests IS NULL OR expected_requests >= 0),
  expected_basis     TEXT,
  note               TEXT,
  UNIQUE (job, scheduled_for_ms),
  CHECK (status NOT IN ('running', 'complete', 'failed') OR started_at_ms IS NOT NULL),
  CHECK (status <> 'complete' OR completed_at_ms IS NOT NULL),
  CHECK (completed_at_ms IS NULL OR completed_at_ms >= started_at_ms),
  CHECK (status <> 'complete' OR expected_requests IS NOT NULL),
  CHECK (expected_requests IS NULL OR expected_basis IS NOT NULL),
  CHECK (expected_requests IS NOT 0 OR note IS NOT NULL)
) STRICT;

-- ---------------------------------------------------------------------
-- RAW layer
-- ---------------------------------------------------------------------
CREATE TABLE api_request (
  request_id        INTEGER PRIMARY KEY,
  slot_id           INTEGER NOT NULL REFERENCES capture_slot(slot_id),
  provider          TEXT    NOT NULL,
  endpoint          TEXT    NOT NULL,
  params_canonical  TEXT    NOT NULL,
  expected_pages    INTEGER CHECK (expected_pages IS NULL OR expected_pages >= 1),
  status            TEXT    NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'complete', 'failed')),
  FOREIGN KEY (provider, endpoint) REFERENCES endpoint_allowlist(provider, endpoint),
  UNIQUE (slot_id, provider, endpoint, params_canonical)
) STRICT;

-- The archive file is a self-describing envelope (params, timestamps,
-- headers, body), so this index can be rebuilt from the archive.
CREATE TABLE raw_response (
  raw_id           INTEGER PRIMARY KEY,
  request_id       INTEGER NOT NULL REFERENCES api_request(request_id),
  page             INTEGER NOT NULL CHECK (page >= 1),
  attempt          INTEGER NOT NULL DEFAULT 1 CHECK (attempt >= 1),
  paged            INTEGER NOT NULL CHECK (paged IN (0, 1)),
  sha256           TEXT    NOT NULL CHECK (length(sha256) = 64),     -- of the body bytes
  archive_path     TEXT    NOT NULL,
  http_status      INTEGER NOT NULL,
  requested_at_ms  INTEGER NOT NULL,
  received_at_ms   INTEGER NOT NULL,
  server_date_ms   INTEGER,
  body_bytes       INTEGER NOT NULL CHECK (body_bytes >= 0),
  api_errors_json  TEXT,
  results_count    INTEGER,
  paging_current   INTEGER,
  paging_total     INTEGER,
  clock_ok INTEGER GENERATED ALWAYS AS (
    CASE WHEN server_date_ms IS NULL THEN 0
         WHEN server_date_ms BETWEEN requested_at_ms - 5000 AND received_at_ms + 5000 THEN 1
         ELSE 0 END) VIRTUAL,
  usable INTEGER GENERATED ALWAYS AS (
    CASE WHEN http_status = 200
          AND api_errors_json IS '[]'
          AND received_at_ms >= requested_at_ms
          AND server_date_ms IS NOT NULL
          AND server_date_ms BETWEEN requested_at_ms - 5000 AND received_at_ms + 5000
          AND (paged = 0 OR (paging_current IS page AND paging_total IS NOT NULL AND paging_total >= page))
         THEN 1 ELSE 0 END) VIRTUAL,
  UNIQUE (request_id, page, attempt)
) STRICT;
CREATE INDEX raw_response_sha ON raw_response(sha256);

CREATE TRIGGER raw_response_paged_matches_provider
BEFORE INSERT ON raw_response
BEGIN
  SELECT RAISE(ABORT, 'raw_response: paged flag differs from provider')
   WHERE NEW.paged IS NOT (SELECT p.paged FROM api_request a JOIN provider p ON p.provider = a.provider
                            WHERE a.request_id = NEW.request_id);
END;

CREATE TRIGGER api_request_complete_requires_all_pages
BEFORE UPDATE OF status ON api_request
WHEN NEW.status = 'complete'
BEGIN
  SELECT RAISE(ABORT, 'api_request: expected_pages unset')
   WHERE NEW.expected_pages IS NULL;
  SELECT RAISE(ABORT, 'api_request: usable pages disagree on paging_total')
   WHERE EXISTS (SELECT 1 FROM raw_response r
                  WHERE r.request_id = NEW.request_id AND r.usable = 1 AND r.paged = 1
                    AND r.paging_total <> NEW.expected_pages);
  SELECT RAISE(ABORT, 'api_request: a page has no usable attempt')
   WHERE (SELECT count(DISTINCT r.page) FROM raw_response r
           WHERE r.request_id = NEW.request_id AND r.usable = 1
             AND r.page BETWEEN 1 AND NEW.expected_pages) <> NEW.expected_pages;
END;

CREATE TRIGGER capture_slot_complete_requires_all_requests
BEFORE UPDATE OF status ON capture_slot
WHEN NEW.status = 'complete'
BEGIN
  SELECT RAISE(ABORT, 'capture_slot: completed requests != expected_requests')
   WHERE (SELECT count(*) FROM api_request a
           WHERE a.slot_id = NEW.slot_id AND a.status = 'complete') <> NEW.expected_requests;
  SELECT RAISE(ABORT, 'capture_slot: has open or failed requests')
   WHERE EXISTS (SELECT 1 FROM api_request a WHERE a.slot_id = NEW.slot_id AND a.status <> 'complete');
END;

-- Quota observations from OddsPapi /v4/account (unmetered).
CREATE TABLE quota_obs (
  raw_id         INTEGER PRIMARY KEY REFERENCES raw_response(raw_id),
  provider       TEXT    NOT NULL REFERENCES provider(provider),
  request_limit  INTEGER NOT NULL CHECK (request_limit >= 0),
  request_count  INTEGER NOT NULL CHECK (request_count >= 0),
  observed_at_ms INTEGER NOT NULL
) STRICT;

-- ---------------------------------------------------------------------
-- Coverage ledger (denominators from the fixture table)
-- ---------------------------------------------------------------------
CREATE TABLE coverage_expectation (
  prereg_id      INTEGER NOT NULL REFERENCES preregistration(prereg_id),
  provider       TEXT    NOT NULL REFERENCES provider(provider),
  bookmaker_id   INTEGER NOT NULL REFERENCES bookmaker(bookmaker_id),
  market_family  TEXT    NOT NULL,
  min_share      REAL    NOT NULL CHECK (min_share > 0.0 AND min_share <= 1.0),
  PRIMARY KEY (prereg_id, provider, bookmaker_id, market_family)
) STRICT;

CREATE TABLE coverage_check (
  day_start_ms        INTEGER NOT NULL,
  prereg_id           INTEGER NOT NULL,
  provider            TEXT    NOT NULL,
  bookmaker_id        INTEGER NOT NULL,
  market_family       TEXT    NOT NULL,
  denominator         INTEGER NOT NULL CHECK (denominator >= 0),
  observed            INTEGER NOT NULL CHECK (observed >= 0),
  min_share           REAL    NOT NULL,
  no_fixtures_reason  TEXT,
  passed INTEGER GENERATED ALWAYS AS (
    CASE WHEN denominator = 0 THEN (no_fixtures_reason IS NOT NULL)
         ELSE (observed >= min_share * denominator) END) VIRTUAL,
  FOREIGN KEY (prereg_id, provider, bookmaker_id, market_family)
    REFERENCES coverage_expectation(prereg_id, provider, bookmaker_id, market_family),
  CHECK (observed <= denominator),
  UNIQUE (day_start_ms, prereg_id, provider, bookmaker_id, market_family)
) STRICT;

CREATE TRIGGER coverage_check_min_share_matches_expectation
BEFORE INSERT ON coverage_check
BEGIN
  SELECT RAISE(ABORT, 'coverage_check: min_share differs from the pre-registered expectation')
   WHERE NEW.min_share IS NOT (SELECT min_share FROM coverage_expectation e
                                WHERE e.prereg_id = NEW.prereg_id AND e.provider = NEW.provider
                                  AND e.bookmaker_id = NEW.bookmaker_id
                                  AND e.market_family = NEW.market_family);
END;

-- ---------------------------------------------------------------------
-- Fixtures (api-football is the master schedule)
-- ---------------------------------------------------------------------
CREATE TABLE fixture (
  fixture_id         INTEGER PRIMARY KEY,
  league_id          INTEGER NOT NULL,
  season             INTEGER NOT NULL,
  round              TEXT    NOT NULL,
  home_team_id       INTEGER NOT NULL REFERENCES team(team_id),
  away_team_id       INTEGER NOT NULL REFERENCES team(team_id),
  first_seen_raw_id  INTEGER NOT NULL REFERENCES raw_response(raw_id),
  et_possible        INTEGER NOT NULL CHECK (et_possible IN (0, 1)),   -- decided pre-match
  et_basis           TEXT    NOT NULL,
  FOREIGN KEY (league_id, season) REFERENCES league(league_id, season),
  CHECK (home_team_id <> away_team_id)
) STRICT;

CREATE TABLE fixture_state_obs (
  obs_id          INTEGER PRIMARY KEY,
  fixture_id      INTEGER NOT NULL REFERENCES fixture(fixture_id),
  raw_id          INTEGER NOT NULL REFERENCES raw_response(raw_id),
  observed_at_ms  INTEGER NOT NULL,
  kickoff_ms      INTEGER NOT NULL,
  status_short    TEXT    NOT NULL CHECK (status_short IN (
                    'TBD', 'NS', '1H', 'HT', '2H', 'ET', 'BT', 'P', 'SUSP', 'INT',
                    'FT', 'AET', 'PEN', 'PST', 'CANC', 'ABD', 'AWD', 'WO', 'LIVE')),
  UNIQUE (fixture_id, raw_id)
) STRICT;
CREATE INDEX fixture_state_obs_lookup ON fixture_state_obs(fixture_id, observed_at_ms);

CREATE VIEW v_fixture_latest AS
SELECT o.fixture_id, o.kickoff_ms, o.status_short, o.observed_at_ms
  FROM fixture_state_obs o
 WHERE o.obs_id = (SELECT o2.obs_id FROM fixture_state_obs o2 WHERE o2.fixture_id = o.fixture_id
                    ORDER BY o2.observed_at_ms DESC, o2.obs_id DESC LIMIT 1);

-- ---------------------------------------------------------------------
-- Cross-provider fixture mapping (requirement 3)
-- ---------------------------------------------------------------------
-- Exact team allowlist: provider participant id -> api-football team id,
-- one-to-one in both directions. The provider's name is kept as evidence.
CREATE TABLE provider_team_map (
  provider            TEXT    NOT NULL REFERENCES provider(provider),
  provider_team_key   TEXT    NOT NULL,        -- e.g. OddsPapi participantId as text
  provider_team_name  TEXT    NOT NULL,
  team_id             INTEGER NOT NULL REFERENCES team(team_id),
  approved_in_commit  TEXT    NOT NULL CHECK (length(approved_in_commit) = 40),
  PRIMARY KEY (provider, provider_team_key),
  UNIQUE (provider, team_id)
) STRICT;

-- Every fixture the provider listed, as parsed from its raw response.
CREATE TABLE provider_fixture_candidate (
  provider             TEXT    NOT NULL REFERENCES provider(provider),
  provider_fixture_id  TEXT    NOT NULL,
  raw_id               INTEGER NOT NULL REFERENCES raw_response(raw_id),
  tournament_key       TEXT    NOT NULL,
  home_team_key        TEXT    NOT NULL,
  away_team_key        TEXT    NOT NULL,
  home_team_name       TEXT    NOT NULL,
  away_team_name       TEXT    NOT NULL,
  start_ms             INTEGER NOT NULL,
  status_key           TEXT,
  PRIMARY KEY (provider, provider_fixture_id, raw_id)
) STRICT;

CREATE TABLE provider_fixture_map (
  provider             TEXT    NOT NULL REFERENCES provider(provider),
  provider_fixture_id  TEXT    NOT NULL,
  fixture_id           INTEGER NOT NULL REFERENCES fixture(fixture_id),
  home_team_key        TEXT    NOT NULL,
  away_team_key        TEXT    NOT NULL,
  provider_start_ms    INTEGER NOT NULL,
  kickoff_at_map_ms    INTEGER NOT NULL,
  evidence             TEXT    NOT NULL CHECK (length(evidence) > 0),
  mapped_at_ms         INTEGER NOT NULL,
  revoked              INTEGER NOT NULL DEFAULT 0 CHECK (revoked IN (0, 1)),
  revoked_reason       TEXT,
  PRIMARY KEY (provider, provider_fixture_id),
  CHECK (abs(provider_start_ms - kickoff_at_map_ms) <= 15 * 60000),
  CHECK ((revoked = 1) = (revoked_reason IS NOT NULL))
) STRICT;
-- One live mapping per api-football fixture per provider (the other direction
-- is the primary key). A revoked mapping frees the fixture for a reissued id.
CREATE UNIQUE INDEX provider_fixture_map_one_live ON provider_fixture_map(provider, fixture_id) WHERE revoked = 0;

CREATE TRIGGER provider_fixture_map_guards
BEFORE INSERT ON provider_fixture_map
BEGIN
  SELECT RAISE(ABORT, 'provider_fixture_map: home team not in exact allowlist or not the fixture home team')
   WHERE (SELECT t.team_id FROM provider_team_map t
           WHERE t.provider = NEW.provider AND t.provider_team_key = NEW.home_team_key)
         IS NOT (SELECT home_team_id FROM fixture WHERE fixture_id = NEW.fixture_id);
  SELECT RAISE(ABORT, 'provider_fixture_map: away team not in exact allowlist or not the fixture away team')
   WHERE (SELECT t.team_id FROM provider_team_map t
           WHERE t.provider = NEW.provider AND t.provider_team_key = NEW.away_team_key)
         IS NOT (SELECT away_team_id FROM fixture WHERE fixture_id = NEW.fixture_id);
  SELECT RAISE(ABORT, 'provider_fixture_map: kickoff_at_map_ms is not the latest known kickoff')
   WHERE NEW.kickoff_at_map_ms IS NOT (SELECT kickoff_ms FROM v_fixture_latest WHERE fixture_id = NEW.fixture_id);
  SELECT RAISE(ABORT, 'provider_fixture_map: no provider candidate with these teams and start time')
   WHERE NOT EXISTS (SELECT 1 FROM provider_fixture_candidate c
                      WHERE c.provider = NEW.provider AND c.provider_fixture_id = NEW.provider_fixture_id
                        AND c.home_team_key = NEW.home_team_key AND c.away_team_key = NEW.away_team_key
                        AND c.start_ms = NEW.provider_start_ms);
END;

-- Independent checks of mappings. The mis-join rate is measured here.
CREATE TABLE mapping_audit (
  audit_id             INTEGER PRIMARY KEY,
  provider             TEXT    NOT NULL,
  provider_fixture_id  TEXT    NOT NULL,
  method               TEXT    NOT NULL CHECK (method IN ('final_score', 'kickoff_recheck', 'price_agreement', 'manual')),
  agrees               INTEGER NOT NULL CHECK (agrees IN (0, 1)),
  detail               TEXT    NOT NULL,
  evidence_raw_id      INTEGER REFERENCES raw_response(raw_id),
  evidence_sha256      TEXT    CHECK (evidence_sha256 IS NULL OR length(evidence_sha256) = 64),
  recorded_at_ms       INTEGER NOT NULL,
  FOREIGN KEY (provider, provider_fixture_id) REFERENCES provider_fixture_map(provider, provider_fixture_id),
  CHECK (evidence_raw_id IS NOT NULL OR evidence_sha256 IS NOT NULL),
  UNIQUE (provider, provider_fixture_id, method)
) STRICT;

CREATE VIEW v_misjoin_rate AS
SELECT provider, method, count(*) AS checked, sum(1 - agrees) AS disagreements,
       1.0 * sum(1 - agrees) / count(*) AS rate
  FROM mapping_audit GROUP BY provider, method;

-- ---------------------------------------------------------------------
-- Parsed prices
-- ---------------------------------------------------------------------
CREATE TABLE parse_run (
  parse_run_id          INTEGER PRIMARY KEY,
  provider              TEXT    NOT NULL REFERENCES provider(provider),
  kind                  TEXT    NOT NULL CHECK (kind IN ('snapshot', 'history')),
  parser_git_commit     TEXT    NOT NULL CHECK (length(parser_git_commit) = 40),
  started_at_ms         INTEGER NOT NULL,
  status                TEXT    NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'complete', 'failed')),
  declared_row_count    INTEGER CHECK (declared_row_count IS NULL OR declared_row_count >= 0),
  empty_reason          TEXT,
  is_current            INTEGER NOT NULL DEFAULT 0 CHECK (is_current IN (0, 1)),
  CHECK (is_current = 0 OR status = 'complete'),
  CHECK (status <> 'complete' OR declared_row_count IS NOT NULL),
  CHECK (declared_row_count IS NOT 0 OR empty_reason IS NOT NULL)
) STRICT;
CREATE UNIQUE INDEX parse_run_one_current ON parse_run(provider, kind) WHERE is_current = 1;

-- Snapshot quotes: a price observed by OUR capture at captured_at_ms.
CREATE TABLE quote (
  quote_id               INTEGER PRIMARY KEY,
  parse_run_id           INTEGER NOT NULL REFERENCES parse_run(parse_run_id),
  raw_id                 INTEGER NOT NULL REFERENCES raw_response(raw_id),
  provider               TEXT    NOT NULL,
  fixture_id             INTEGER NOT NULL REFERENCES fixture(fixture_id),
  bookmaker_id           INTEGER NOT NULL REFERENCES bookmaker(bookmaker_id),
  provider_market_label  TEXT    NOT NULL,
  value_label            TEXT    NOT NULL,
  market_key             TEXT    NOT NULL,
  selection              TEXT    NOT NULL,
  line_q                 INTEGER NOT NULL,
  price                  REAL    NOT NULL CHECK (price > 1.0 AND price <= 1000.0),
  source_update_ms       INTEGER,
  captured_at_ms         INTEGER NOT NULL,
  kickoff_in_payload_ms  INTEGER,
  FOREIGN KEY (provider, provider_market_label, market_key)
    REFERENCES provider_market_allowlist(provider, provider_market_label, market_key),
  FOREIGN KEY (provider, market_key, value_label, selection, line_q)
    REFERENCES value_allowlist(provider, market_key, value_label, selection, line_q),
  FOREIGN KEY (provider, bookmaker_id) REFERENCES provider_bookmaker_map(provider, bookmaker_id),
  CHECK (kickoff_in_payload_ms IS NULL OR captured_at_ms < kickoff_in_payload_ms),
  CHECK (source_update_ms IS NULL OR source_update_ms <= captured_at_ms + 5000),
  UNIQUE (parse_run_id, raw_id, fixture_id, bookmaker_id, market_key, line_q, selection)
) STRICT;
CREATE INDEX quote_lookup ON quote(fixture_id, market_key, bookmaker_id, captured_at_ms);

CREATE TRIGGER quote_parse_layer_guards
BEFORE INSERT ON quote
BEGIN
  SELECT RAISE(ABORT, 'quote: raw response is not usable (quarantine it instead)')
   WHERE (SELECT usable FROM raw_response WHERE raw_id = NEW.raw_id) IS NOT 1;
  SELECT RAISE(ABORT, 'quote: provider differs from the raw response''s provider')
   WHERE NEW.provider IS NOT (SELECT a.provider FROM raw_response r JOIN api_request a ON a.request_id = r.request_id
                               WHERE r.raw_id = NEW.raw_id);
  SELECT RAISE(ABORT, 'quote: parse run is not a running snapshot run for this provider')
   WHERE NOT EXISTS (SELECT 1 FROM parse_run p WHERE p.parse_run_id = NEW.parse_run_id
                        AND p.provider = NEW.provider AND p.kind = 'snapshot' AND p.status = 'running');
  SELECT RAISE(ABORT, 'quote: captured_at_ms must equal raw_response.received_at_ms')
   WHERE NEW.captured_at_ms IS NOT (SELECT received_at_ms FROM raw_response WHERE raw_id = NEW.raw_id);
  SELECT RAISE(ABORT, 'quote: kickoff already passed per fixture table at capture')
   WHERE NEW.captured_at_ms >= (SELECT o.kickoff_ms FROM fixture_state_obs o
                                 WHERE o.fixture_id = NEW.fixture_id AND o.observed_at_ms <= NEW.captured_at_ms
                                 ORDER BY o.observed_at_ms DESC, o.obs_id DESC LIMIT 1);
END;

-- Vendor price history: a price the VENDOR says existed from valid_from_ms.
-- Fetched after the event. It may serve as the closing-price benchmark and as
-- exploration data, never as a model input for a forward (confirmation) pick.
CREATE TABLE price_history (
  hist_id                INTEGER PRIMARY KEY,
  parse_run_id           INTEGER NOT NULL REFERENCES parse_run(parse_run_id),
  raw_id                 INTEGER NOT NULL REFERENCES raw_response(raw_id),
  provider               TEXT    NOT NULL,
  provider_fixture_id    TEXT    NOT NULL,
  fixture_id             INTEGER NOT NULL REFERENCES fixture(fixture_id),
  bookmaker_id           INTEGER NOT NULL REFERENCES bookmaker(bookmaker_id),
  provider_market_label  TEXT    NOT NULL,
  value_label            TEXT    NOT NULL,
  market_key             TEXT    NOT NULL,
  selection              TEXT    NOT NULL,
  line_q                 INTEGER NOT NULL,
  valid_from_ms          INTEGER NOT NULL,
  price                  REAL    NOT NULL CHECK (price > 1.0 AND price <= 1000.0),
  active                 INTEGER NOT NULL CHECK (active IN (0, 1)),
  max_stake              REAL,
  FOREIGN KEY (provider, provider_market_label, market_key)
    REFERENCES provider_market_allowlist(provider, provider_market_label, market_key),
  FOREIGN KEY (provider, market_key, value_label, selection, line_q)
    REFERENCES value_allowlist(provider, market_key, value_label, selection, line_q),
  FOREIGN KEY (provider, bookmaker_id) REFERENCES provider_bookmaker_map(provider, bookmaker_id),
  FOREIGN KEY (provider, provider_fixture_id) REFERENCES provider_fixture_map(provider, provider_fixture_id),
  UNIQUE (parse_run_id, fixture_id, bookmaker_id, market_key, line_q, selection, valid_from_ms)
) STRICT;
CREATE INDEX price_history_lookup ON price_history(fixture_id, bookmaker_id, market_key, line_q, selection, valid_from_ms);

CREATE TRIGGER price_history_guards
BEFORE INSERT ON price_history
BEGIN
  SELECT RAISE(ABORT, 'price_history: raw response is not usable')
   WHERE (SELECT usable FROM raw_response WHERE raw_id = NEW.raw_id) IS NOT 1;
  SELECT RAISE(ABORT, 'price_history: provider differs from the raw response''s provider')
   WHERE NEW.provider IS NOT (SELECT a.provider FROM raw_response r JOIN api_request a ON a.request_id = r.request_id
                               WHERE r.raw_id = NEW.raw_id);
  SELECT RAISE(ABORT, 'price_history: parse run is not a running history run for this provider')
   WHERE NOT EXISTS (SELECT 1 FROM parse_run p WHERE p.parse_run_id = NEW.parse_run_id
                        AND p.provider = NEW.provider AND p.kind = 'history' AND p.status = 'running');
  SELECT RAISE(ABORT, 'price_history: fixture_id differs from a live provider mapping')
   WHERE NEW.fixture_id IS NOT (SELECT m.fixture_id FROM provider_fixture_map m
                                 WHERE m.provider = NEW.provider AND m.provider_fixture_id = NEW.provider_fixture_id
                                   AND m.revoked = 0);
  SELECT RAISE(ABORT, 'price_history: record timestamp after the response was received')
   WHERE NEW.valid_from_ms > (SELECT received_at_ms FROM raw_response WHERE raw_id = NEW.raw_id);
END;

-- Closing price: the last ACTIVE record strictly before the final known kickoff.
-- Also exposes how recently the vendor recorded ANY record for that book and
-- fixture before kickoff (observation activity), which G0.5 uses.
CREATE VIEW v_close AS
WITH cur AS (SELECT h.* FROM price_history h
               JOIN parse_run p ON p.parse_run_id = h.parse_run_id AND p.is_current = 1),
ko AS (SELECT fixture_id, kickoff_ms FROM v_fixture_latest),
pre AS (SELECT cur.*, ko.kickoff_ms FROM cur JOIN ko USING (fixture_id) WHERE cur.valid_from_ms < ko.kickoff_ms),
last_active AS (
  SELECT fixture_id, bookmaker_id, market_key, line_q, selection, max(valid_from_ms) AS valid_from_ms
    FROM pre WHERE active = 1 GROUP BY fixture_id, bookmaker_id, market_key, line_q, selection),
activity AS (
  SELECT fixture_id, bookmaker_id, max(valid_from_ms) AS last_any_record_ms, min(kickoff_ms) AS kickoff_ms
    FROM pre GROUP BY fixture_id, bookmaker_id)
SELECT la.fixture_id, la.bookmaker_id, la.market_key, la.line_q, la.selection,
       pre.price AS close_price, la.valid_from_ms AS close_valid_from_ms,
       a.kickoff_ms, a.last_any_record_ms,
       a.kickoff_ms - la.valid_from_ms AS market_age_at_ko_ms,
       a.kickoff_ms - a.last_any_record_ms AS book_silence_at_ko_ms
  FROM last_active la
  JOIN pre ON pre.fixture_id = la.fixture_id AND pre.bookmaker_id = la.bookmaker_id
          AND pre.market_key = la.market_key AND pre.line_q = la.line_q
          AND pre.selection = la.selection AND pre.valid_from_ms = la.valid_from_ms
  JOIN activity a ON a.fixture_id = la.fixture_id AND a.bookmaker_id = la.bookmaker_id;

-- Quarantine (per raw response, book and market label) instead of global blocking.
CREATE TABLE unmapped_label (
  parse_run_id           INTEGER NOT NULL REFERENCES parse_run(parse_run_id),
  raw_id                 INTEGER NOT NULL REFERENCES raw_response(raw_id),
  bookmaker_id           INTEGER NOT NULL REFERENCES bookmaker(bookmaker_id),
  provider_market_label  TEXT    NOT NULL,
  value_label            TEXT    NOT NULL,
  in_allowlisted_market  INTEGER NOT NULL CHECK (in_allowlisted_market IN (0, 1)),
  PRIMARY KEY (parse_run_id, raw_id, bookmaker_id, provider_market_label, value_label)
) STRICT;

CREATE TABLE quarantine (
  parse_run_id           INTEGER NOT NULL REFERENCES parse_run(parse_run_id),
  raw_id                 INTEGER NOT NULL REFERENCES raw_response(raw_id),
  bookmaker_id           INTEGER NOT NULL REFERENCES bookmaker(bookmaker_id),
  provider_market_label  TEXT    NOT NULL,
  reason                 TEXT    NOT NULL CHECK (reason IN (
                           'incomplete_group', 'overround_out_of_range', 'unknown_label', 'other')),
  detail                 TEXT    NOT NULL,
  PRIMARY KEY (parse_run_id, raw_id, bookmaker_id, provider_market_label, reason)
) STRICT;

CREATE VIEW v_bad_groups AS
SELECT q.parse_run_id, q.raw_id, q.bookmaker_id, q.provider_market_label, q.market_key, q.line_q,
       count(*) AS n, count(DISTINCT q.selection) AS n_sel, sum(1.0 / q.price) AS book_sum,
       CASE m.structure WHEN 'three_way' THEN 3
            WHEN 'ou_half' THEN 2 WHEN 'ou_whole' THEN 2 WHEN 'ou_quarter' THEN 2
            WHEN 'yes_no' THEN 2 WHEN 'handicap_half' THEN 2 WHEN 'handicap_whole' THEN 2
            WHEN 'handicap_quarter' THEN 2 WHEN 'two_way_other' THEN 2 WHEN 'odd_even' THEN 2
            ELSE NULL END AS expected_n,
       CASE m.structure WHEN 'three_way' THEN 1.35 ELSE 1.25 END AS max_sum
  FROM quote q JOIN market_def m ON m.market_key = q.market_key
 GROUP BY q.parse_run_id, q.raw_id, q.bookmaker_id, q.provider_market_label, q.market_key, q.line_q
HAVING expected_n IS NOT NULL
   AND (n <> expected_n OR n_sel <> expected_n OR book_sum < 1.0 OR book_sum > max_sum);

CREATE TRIGGER parse_run_complete_guards
BEFORE UPDATE OF status ON parse_run
WHEN NEW.status = 'complete'
BEGIN
  SELECT RAISE(ABORT, 'parse_run: row count != declared_row_count')
   WHERE (CASE NEW.kind WHEN 'snapshot' THEN (SELECT count(*) FROM quote q WHERE q.parse_run_id = NEW.parse_run_id)
                        ELSE (SELECT count(*) FROM price_history h WHERE h.parse_run_id = NEW.parse_run_id) END)
         IS NOT NEW.declared_row_count;
  SELECT RAISE(ABORT, 'parse_run: bad group not quarantined')
   WHERE EXISTS (SELECT 1 FROM v_bad_groups g
                  WHERE g.parse_run_id = NEW.parse_run_id
                    AND NOT EXISTS (SELECT 1 FROM quarantine z
                                     WHERE z.parse_run_id = g.parse_run_id AND z.raw_id = g.raw_id
                                       AND z.bookmaker_id = g.bookmaker_id
                                       AND z.provider_market_label = g.provider_market_label));
  SELECT RAISE(ABORT, 'parse_run: unknown label in an allowlisted market not quarantined')
   WHERE EXISTS (SELECT 1 FROM unmapped_label u
                  WHERE u.parse_run_id = NEW.parse_run_id AND u.in_allowlisted_market = 1
                    AND NOT EXISTS (SELECT 1 FROM quarantine z
                                     WHERE z.parse_run_id = u.parse_run_id AND z.raw_id = u.raw_id
                                       AND z.bookmaker_id = u.bookmaker_id
                                       AND z.provider_market_label = u.provider_market_label
                                       AND z.reason = 'unknown_label'));
END;

CREATE VIEW v_quote_clean AS
SELECT q.* FROM quote q
  JOIN parse_run p ON p.parse_run_id = q.parse_run_id AND p.is_current = 1
 WHERE NOT EXISTS (SELECT 1 FROM quarantine z
                    WHERE z.parse_run_id = q.parse_run_id AND z.raw_id = q.raw_id
                      AND z.bookmaker_id = q.bookmaker_id
                      AND z.provider_market_label = q.provider_market_label);

-- ---------------------------------------------------------------------
-- Forward evaluations, confirmatory rules (capped), picks
-- Backtests on history are NOT stored here: they are exploration outputs.
-- An evaluation row is a forecast frozen at wall-clock time (checked by the
-- database clock), so it cannot be backfilled after the event.
-- ---------------------------------------------------------------------
CREATE TABLE model_version (
  model_version  TEXT    PRIMARY KEY,
  git_commit     TEXT    NOT NULL CHECK (length(git_commit) = 40),
  prereg_id      INTEGER NOT NULL REFERENCES preregistration(prereg_id)
) STRICT;

CREATE TABLE evaluation (
  eval_id                 INTEGER PRIMARY KEY,
  model_version           TEXT    NOT NULL REFERENCES model_version(model_version),
  fixture_id              INTEGER NOT NULL REFERENCES fixture(fixture_id),
  market_key              TEXT    NOT NULL REFERENCES market_def(market_key),
  selection               TEXT    NOT NULL,
  line_q                  INTEGER NOT NULL,
  p_model                 REAL    NOT NULL CHECK (p_model > 0.0 AND p_model < 1.0),
  evaluated_at_ms         INTEGER NOT NULL,
  feature_cutoff_ms       INTEGER NOT NULL,     -- latest timestamp of any model input
  kickoff_known_ms        INTEGER NOT NULL,
  ref_quote_id_at_eval    INTEGER REFERENCES quote(quote_id),   -- market at prediction time
  CHECK (feature_cutoff_ms <= evaluated_at_ms),
  CHECK (evaluated_at_ms < kickoff_known_ms),
  UNIQUE (model_version, fixture_id, market_key, line_q, selection)
) STRICT;

CREATE TRIGGER evaluation_no_lookahead
BEFORE INSERT ON evaluation
BEGIN
  SELECT RAISE(ABORT, 'evaluation: evaluated_at_ms is not the database clock time (no backfilled forecasts)')
   WHERE abs(NEW.evaluated_at_ms - CAST(unixepoch('subsec') * 1000 AS INTEGER)) > 120000;
  SELECT RAISE(ABORT, 'evaluation: kickoff_known_ms is not the latest kickoff known at evaluated_at_ms')
   WHERE NEW.kickoff_known_ms IS NOT (
     SELECT o.kickoff_ms FROM fixture_state_obs o
      WHERE o.fixture_id = NEW.fixture_id AND o.observed_at_ms <= NEW.evaluated_at_ms
      ORDER BY o.observed_at_ms DESC, o.obs_id DESC LIMIT 1);
  SELECT RAISE(ABORT, 'evaluation: an outcome for this fixture already existed (look-ahead)')
   WHERE EXISTS (SELECT 1 FROM outcome_obs x WHERE x.fixture_id = NEW.fixture_id
                    AND x.recorded_at_ms <= NEW.evaluated_at_ms);
  SELECT RAISE(ABORT, 'evaluation: reference quote captured after evaluation (look-ahead)')
   WHERE EXISTS (SELECT 1 FROM quote q WHERE q.quote_id = NEW.ref_quote_id_at_eval
                    AND q.captured_at_ms > NEW.evaluated_at_ms);
  SELECT RAISE(ABORT, 'evaluation: reference quote is for another fixture/market/line/selection')
   WHERE EXISTS (SELECT 1 FROM quote q WHERE q.quote_id = NEW.ref_quote_id_at_eval
                    AND (q.fixture_id <> NEW.fixture_id OR q.market_key <> NEW.market_key
                      OR q.line_q <> NEW.line_q OR q.selection <> NEW.selection));
END;

CREATE TABLE strategy (
  strategy_id    TEXT    PRIMARY KEY,
  prereg_id      INTEGER NOT NULL REFERENCES preregistration(prereg_id),
  kind           TEXT    NOT NULL CHECK (kind IN ('rule', 'control')),
  confirm_slot   INTEGER UNIQUE CHECK (confirm_slot BETWEEN 1 AND 6),
  description    TEXT    NOT NULL,
  CHECK ((kind = 'rule') = (confirm_slot IS NOT NULL))
) STRICT;

CREATE TABLE pick (
  strategy_id   TEXT    NOT NULL REFERENCES strategy(strategy_id),
  fixture_id    INTEGER NOT NULL REFERENCES fixture(fixture_id),
  eval_id       INTEGER NOT NULL REFERENCES evaluation(eval_id),
  frozen_at_ms  INTEGER NOT NULL,
  PRIMARY KEY (strategy_id, fixture_id)
) STRICT;

CREATE TRIGGER pick_freeze_rules
BEFORE INSERT ON pick
BEGIN
  SELECT RAISE(ABORT, 'pick: frozen_at_ms is not the database clock time')
   WHERE abs(NEW.frozen_at_ms - CAST(unixepoch('subsec') * 1000 AS INTEGER)) > 120000;
  SELECT RAISE(ABORT, 'pick: evaluation is for a different fixture')
   WHERE (SELECT fixture_id FROM evaluation WHERE eval_id = NEW.eval_id) <> NEW.fixture_id;
  SELECT RAISE(ABORT, 'pick: frozen before its evaluation existed')
   WHERE NEW.frozen_at_ms < (SELECT evaluated_at_ms FROM evaluation WHERE eval_id = NEW.eval_id);
  SELECT RAISE(ABORT, 'pick: frozen at or after kickoff as known')
   WHERE NEW.frozen_at_ms >= (SELECT o.kickoff_ms FROM fixture_state_obs o
                               WHERE o.fixture_id = NEW.fixture_id AND o.observed_at_ms <= NEW.frozen_at_ms
                               ORDER BY o.observed_at_ms DESC, o.obs_id DESC LIMIT 1);
  SELECT RAISE(ABORT, 'pick: frozen before the strategy''s pre-registration took effect')
   WHERE NEW.frozen_at_ms < (SELECT p.effective_from_ms FROM strategy s
                               JOIN preregistration p ON p.prereg_id = s.prereg_id
                              WHERE s.strategy_id = NEW.strategy_id);
END;

-- ---------------------------------------------------------------------
-- Bet slips: the ONLY execution data. One row per SportyBet slip actually
-- placed, transcribed from the slip with a screenshot hash. Singles only.
-- ---------------------------------------------------------------------
CREATE TABLE bet_slip (
  slip_id                INTEGER PRIMARY KEY,
  ticket_id              TEXT    NOT NULL UNIQUE,
  placed_at_ms           INTEGER NOT NULL,
  fixture_id             INTEGER NOT NULL REFERENCES fixture(fixture_id),
  market_key             TEXT    NOT NULL REFERENCES market_def(market_key),
  selection              TEXT    NOT NULL,
  line_q                 INTEGER NOT NULL,
  odds_taken             REAL    NOT NULL CHECK (odds_taken > 1.0),
  stake_minor            INTEGER NOT NULL CHECK (stake_minor > 0),          -- kobo
  legs                   INTEGER NOT NULL CHECK (legs = 1),
  strategy_id            TEXT,
  min_acceptable_price   REAL    CHECK (min_acceptable_price IS NULL OR min_acceptable_price > 1.0),
  slip_screenshot_sha256 TEXT    NOT NULL CHECK (length(slip_screenshot_sha256) = 64),
  recorded_at_ms         INTEGER NOT NULL,
  FOREIGN KEY (strategy_id, fixture_id) REFERENCES pick(strategy_id, fixture_id),
  CHECK (recorded_at_ms >= placed_at_ms)
) STRICT;

CREATE TRIGGER bet_slip_timing
BEFORE INSERT ON bet_slip
BEGIN
  SELECT RAISE(ABORT, 'bet_slip: placed at or after kickoff as known')
   WHERE NEW.placed_at_ms >= (SELECT o.kickoff_ms FROM fixture_state_obs o
                               WHERE o.fixture_id = NEW.fixture_id AND o.observed_at_ms <= NEW.placed_at_ms
                               ORDER BY o.observed_at_ms DESC, o.obs_id DESC LIMIT 1);
  SELECT RAISE(ABORT, 'bet_slip: placed before the linked pick was frozen')
   WHERE NEW.strategy_id IS NOT NULL
     AND NEW.placed_at_ms < (SELECT frozen_at_ms FROM pick WHERE strategy_id = NEW.strategy_id AND fixture_id = NEW.fixture_id);
END;

CREATE TABLE bet_settlement (
  slip_id            INTEGER PRIMARY KEY REFERENCES bet_slip(slip_id),
  result             TEXT    NOT NULL CHECK (result IN ('win', 'lose', 'push', 'half_win', 'half_lose', 'void', 'cashout')),
  returned_minor     INTEGER NOT NULL CHECK (returned_minor >= 0),
  evidence_sha256    TEXT    NOT NULL CHECK (length(evidence_sha256) = 64),
  recorded_at_ms     INTEGER NOT NULL
) STRICT;

-- ---------------------------------------------------------------------
-- Outcomes and evaluation labels
-- ---------------------------------------------------------------------
CREATE TABLE outcome_obs (
  outcome_id      INTEGER PRIMARY KEY,
  fixture_id      INTEGER NOT NULL REFERENCES fixture(fixture_id),
  source          TEXT    NOT NULL CHECK (source IN ('api_football', 'second_source_manual')),
  raw_id          INTEGER REFERENCES raw_response(raw_id),
  evidence_sha256 TEXT    CHECK (evidence_sha256 IS NULL OR length(evidence_sha256) = 64),
  recorded_at_ms  INTEGER NOT NULL,
  status_short    TEXT    NOT NULL,
  goals_home_90   INTEGER CHECK (goals_home_90 IS NULL OR goals_home_90 >= 0),
  goals_away_90   INTEGER CHECK (goals_away_90 IS NULL OR goals_away_90 >= 0),
  corners_home    INTEGER CHECK (corners_home IS NULL OR corners_home BETWEEN 0 AND 40),
  corners_away    INTEGER CHECK (corners_away IS NULL OR corners_away BETWEEN 0 AND 40),
  corners_raw_id  INTEGER REFERENCES raw_response(raw_id),
  CHECK (source <> 'api_football' OR raw_id IS NOT NULL),
  CHECK (source <> 'api_football' OR corners_home IS NULL OR corners_raw_id IS NOT NULL),
  CHECK (source = 'api_football' OR evidence_sha256 IS NOT NULL),
  CHECK ((corners_home IS NULL) = (corners_away IS NULL)),
  CHECK ((goals_home_90 IS NULL) = (goals_away_90 IS NULL)),
  CHECK (status_short NOT IN ('FT', 'AET', 'PEN') OR goals_home_90 IS NOT NULL),
  UNIQUE (fixture_id, source, recorded_at_ms)
) STRICT;

CREATE TABLE eval_label (
  fixture_id       INTEGER NOT NULL REFERENCES fixture(fixture_id),
  market_family    TEXT    NOT NULL,
  rule_version     TEXT    NOT NULL,
  status           TEXT    NOT NULL CHECK (status IN ('included', 'excluded')),
  excluded_reason  TEXT    CHECK (excluded_reason IS NULL OR excluded_reason IN (
                     'ex_ante_et_possible', 'abandoned', 'postponed_or_cancelled', 'stat_missing',
                     'no_usable_close', 'other')),
  outcome_id       INTEGER REFERENCES outcome_obs(outcome_id),
  labelled_at_ms   INTEGER NOT NULL,
  PRIMARY KEY (fixture_id, market_family, rule_version),
  CHECK ((status = 'excluded') = (excluded_reason IS NOT NULL)),
  CHECK (status = 'excluded' OR outcome_id IS NOT NULL)
) STRICT;

CREATE TRIGGER eval_label_guards
BEFORE INSERT ON eval_label
BEGIN
  SELECT RAISE(ABORT, 'eval_label: corner markets on an ET-possible fixture must be excluded ex ante')
   WHERE NEW.market_family = 'corners' AND NEW.status = 'included'
     AND (SELECT et_possible FROM fixture WHERE fixture_id = NEW.fixture_id) = 1;
  SELECT RAISE(ABORT, 'eval_label: ex-ante ET exclusion used on a fixture where ET was not possible')
   WHERE NEW.excluded_reason = 'ex_ante_et_possible'
     AND (SELECT et_possible FROM fixture WHERE fixture_id = NEW.fixture_id) = 0;
  SELECT RAISE(ABORT, 'eval_label: outcome is for another fixture')
   WHERE NEW.outcome_id IS NOT NULL
     AND (SELECT fixture_id FROM outcome_obs WHERE outcome_id = NEW.outcome_id) <> NEW.fixture_id;
  SELECT RAISE(ABORT, 'eval_label: included corner label needs status FT and corner counts')
   WHERE NEW.market_family = 'corners' AND NEW.status = 'included'
     AND NOT EXISTS (SELECT 1 FROM outcome_obs o WHERE o.outcome_id = NEW.outcome_id
                       AND o.status_short = 'FT' AND o.corners_home IS NOT NULL);
  SELECT RAISE(ABORT, 'eval_label: included label needs 90-minute goals')
   WHERE NEW.market_family <> 'corners' AND NEW.status = 'included'
     AND NOT EXISTS (SELECT 1 FROM outcome_obs o WHERE o.outcome_id = NEW.outcome_id
                       AND o.goals_home_90 IS NOT NULL);
END;

-- ---------------------------------------------------------------------
-- Append-only enforcement
-- ---------------------------------------------------------------------
CREATE TRIGGER preregistration_no_update BEFORE UPDATE ON preregistration
  WHEN NOT (OLD.ots_proof_path IS NULL AND NEW.ots_proof_path IS NOT NULL
            AND NEW.prereg_id = OLD.prereg_id AND NEW.title = OLD.title AND NEW.git_commit = OLD.git_commit
            AND NEW.signed_tag = OLD.signed_tag AND NEW.doc_sha256 = OLD.doc_sha256
            AND NEW.committed_at_ms = OLD.committed_at_ms AND NEW.effective_from_ms = OLD.effective_from_ms)
  BEGIN SELECT RAISE(ABORT, 'preregistration is append-only (only ots_proof_path may be filled once)'); END;
CREATE TRIGGER preregistration_no_delete BEFORE DELETE ON preregistration BEGIN SELECT RAISE(ABORT, 'preregistration is append-only'); END;
CREATE TRIGGER raw_response_no_update BEFORE UPDATE ON raw_response BEGIN SELECT RAISE(ABORT, 'raw_response is append-only'); END;
CREATE TRIGGER raw_response_no_delete BEFORE DELETE ON raw_response BEGIN SELECT RAISE(ABORT, 'raw_response is append-only'); END;
CREATE TRIGGER quota_obs_no_update BEFORE UPDATE ON quota_obs BEGIN SELECT RAISE(ABORT, 'quota_obs is append-only'); END;
CREATE TRIGGER quota_obs_no_delete BEFORE DELETE ON quota_obs BEGIN SELECT RAISE(ABORT, 'quota_obs is append-only'); END;
CREATE TRIGGER fixture_state_obs_no_update BEFORE UPDATE ON fixture_state_obs BEGIN SELECT RAISE(ABORT, 'fixture_state_obs is append-only'); END;
CREATE TRIGGER fixture_state_obs_no_delete BEFORE DELETE ON fixture_state_obs BEGIN SELECT RAISE(ABORT, 'fixture_state_obs is append-only'); END;
CREATE TRIGGER provider_fixture_map_no_update BEFORE UPDATE ON provider_fixture_map
  WHEN NOT (OLD.revoked = 0 AND NEW.revoked = 1 AND NEW.revoked_reason IS NOT NULL
            AND NEW.provider IS OLD.provider AND NEW.provider_fixture_id IS OLD.provider_fixture_id
            AND NEW.fixture_id IS OLD.fixture_id AND NEW.home_team_key IS OLD.home_team_key
            AND NEW.away_team_key IS OLD.away_team_key AND NEW.provider_start_ms IS OLD.provider_start_ms
            AND NEW.kickoff_at_map_ms IS OLD.kickoff_at_map_ms AND NEW.evidence IS OLD.evidence
            AND NEW.mapped_at_ms IS OLD.mapped_at_ms)
  BEGIN SELECT RAISE(ABORT, 'provider_fixture_map is append-only (only revocation is allowed)'); END;
CREATE TRIGGER provider_fixture_map_no_delete BEFORE DELETE ON provider_fixture_map BEGIN SELECT RAISE(ABORT, 'provider_fixture_map is append-only'); END;
CREATE TRIGGER mapping_audit_no_update BEFORE UPDATE ON mapping_audit BEGIN SELECT RAISE(ABORT, 'mapping_audit is append-only'); END;
CREATE TRIGGER mapping_audit_no_delete BEFORE DELETE ON mapping_audit BEGIN SELECT RAISE(ABORT, 'mapping_audit is append-only'); END;
CREATE TRIGGER quote_no_update BEFORE UPDATE ON quote BEGIN SELECT RAISE(ABORT, 'quote is append-only'); END;
CREATE TRIGGER quote_no_delete BEFORE DELETE ON quote BEGIN SELECT RAISE(ABORT, 'quote is append-only'); END;
CREATE TRIGGER price_history_no_update BEFORE UPDATE ON price_history BEGIN SELECT RAISE(ABORT, 'price_history is append-only'); END;
CREATE TRIGGER price_history_no_delete BEFORE DELETE ON price_history BEGIN SELECT RAISE(ABORT, 'price_history is append-only'); END;
CREATE TRIGGER coverage_check_no_update BEFORE UPDATE ON coverage_check BEGIN SELECT RAISE(ABORT, 'coverage_check is append-only'); END;
CREATE TRIGGER coverage_check_no_delete BEFORE DELETE ON coverage_check BEGIN SELECT RAISE(ABORT, 'coverage_check is append-only'); END;
CREATE TRIGGER evaluation_no_update BEFORE UPDATE ON evaluation BEGIN SELECT RAISE(ABORT, 'evaluation is frozen'); END;
CREATE TRIGGER evaluation_no_delete BEFORE DELETE ON evaluation BEGIN SELECT RAISE(ABORT, 'evaluation is frozen'); END;
CREATE TRIGGER strategy_no_update BEFORE UPDATE ON strategy BEGIN SELECT RAISE(ABORT, 'strategy is frozen once registered'); END;
CREATE TRIGGER strategy_no_delete BEFORE DELETE ON strategy BEGIN SELECT RAISE(ABORT, 'strategy is frozen once registered'); END;
CREATE TRIGGER pick_no_update BEFORE UPDATE ON pick BEGIN SELECT RAISE(ABORT, 'pick is frozen'); END;
CREATE TRIGGER pick_no_delete BEFORE DELETE ON pick BEGIN SELECT RAISE(ABORT, 'pick is frozen'); END;
CREATE TRIGGER bet_slip_no_update BEFORE UPDATE ON bet_slip BEGIN SELECT RAISE(ABORT, 'bet_slip is append-only'); END;
CREATE TRIGGER bet_slip_no_delete BEFORE DELETE ON bet_slip BEGIN SELECT RAISE(ABORT, 'bet_slip is append-only'); END;
CREATE TRIGGER bet_settlement_no_update BEFORE UPDATE ON bet_settlement BEGIN SELECT RAISE(ABORT, 'bet_settlement is append-only'); END;
CREATE TRIGGER bet_settlement_no_delete BEFORE DELETE ON bet_settlement BEGIN SELECT RAISE(ABORT, 'bet_settlement is append-only'); END;
CREATE TRIGGER outcome_obs_no_update BEFORE UPDATE ON outcome_obs BEGIN SELECT RAISE(ABORT, 'outcome_obs is append-only'); END;
CREATE TRIGGER outcome_obs_no_delete BEFORE DELETE ON outcome_obs BEGIN SELECT RAISE(ABORT, 'outcome_obs is append-only'); END;
CREATE TRIGGER eval_label_no_update BEFORE UPDATE ON eval_label BEGIN SELECT RAISE(ABORT, 'eval_label is append-only; relabel under a new rule_version'); END;
CREATE TRIGGER eval_label_no_delete BEFORE DELETE ON eval_label BEGIN SELECT RAISE(ABORT, 'eval_label is append-only'); END;

PRAGMA user_version = 4;
