-- corners-migration: foreign_keys=off
-- v0.5.7: map a fixture by STRUCTURE when kickoffs are provisional.
--
-- Until now a mapping needed the provider's listed start within 15 minutes of
-- api-football's latest kickoff. Listings refresh every 28 days, so a fixture whose
-- kickoff is fixed or re-timed after a listing (TV picks, provisional placeholder
-- times) couldn't map. That hole isn't random: re-timed matches are the televised ones.
--
-- map_basis = 'structure': both participants are approved, the api-football fixture
-- has exactly those teams on the same sides, that ordered pair occurs exactly ONCE in
-- the league-season (a double round robin, or a UEFA league stage), and the listed
-- start is within 4 days of the kickoff (which pins the season). Orientation is never
-- swapped. 'kickoff' rows keep the 15-minute rule.
--
-- SQLite can't drop a CHECK in place, so the table is rebuilt, following the documented
-- procedure: foreign keys off for this migration (apply_migrations handles the header
-- line above), copy, drop, rename, recreate index and triggers, and foreign_key_check
-- before commit.

CREATE TABLE provider_fixture_map_v8 (
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
  map_basis            TEXT    NOT NULL DEFAULT 'kickoff' CHECK (map_basis IN ('kickoff', 'structure')),
  PRIMARY KEY (provider, provider_fixture_id),
  CHECK (map_basis <> 'kickoff' OR abs(provider_start_ms - kickoff_at_map_ms) <= 15 * 60000),
  CHECK (map_basis <> 'structure' OR abs(provider_start_ms - kickoff_at_map_ms) <= 4 * 86400000),
  CHECK ((revoked = 1) = (revoked_reason IS NOT NULL))
) STRICT;

INSERT INTO provider_fixture_map_v8 (provider, provider_fixture_id, fixture_id, home_team_key, away_team_key,
  provider_start_ms, kickoff_at_map_ms, evidence, mapped_at_ms, revoked, revoked_reason, map_basis)
SELECT provider, provider_fixture_id, fixture_id, home_team_key, away_team_key,
  provider_start_ms, kickoff_at_map_ms, evidence, mapped_at_ms, revoked, revoked_reason, 'kickoff'
FROM provider_fixture_map;

DROP TABLE provider_fixture_map;
PRAGMA legacy_alter_table = ON;      -- other triggers name provider_fixture_map; don't re-validate mid-swap
ALTER TABLE provider_fixture_map_v8 RENAME TO provider_fixture_map;
PRAGMA legacy_alter_table = OFF;

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
  SELECT RAISE(ABORT, 'provider_fixture_map: structure basis needs the ordered pair to occur once in its league-season')
   WHERE NEW.map_basis = 'structure'
     AND (SELECT count(*) FROM fixture f2 JOIN fixture f1 ON f1.fixture_id = NEW.fixture_id
           WHERE f2.league_id = f1.league_id AND f2.season = f1.season
             AND f2.home_team_id = f1.home_team_id AND f2.away_team_id = f1.away_team_id) <> 1;
END;

CREATE TRIGGER provider_fixture_map_no_update BEFORE UPDATE ON provider_fixture_map
  WHEN NOT (OLD.revoked = 0 AND NEW.revoked = 1 AND NEW.revoked_reason IS NOT NULL
            AND NEW.provider IS OLD.provider AND NEW.provider_fixture_id IS OLD.provider_fixture_id
            AND NEW.fixture_id IS OLD.fixture_id AND NEW.home_team_key IS OLD.home_team_key
            AND NEW.away_team_key IS OLD.away_team_key AND NEW.provider_start_ms IS OLD.provider_start_ms
            AND NEW.kickoff_at_map_ms IS OLD.kickoff_at_map_ms AND NEW.evidence IS OLD.evidence
            AND NEW.mapped_at_ms IS OLD.mapped_at_ms AND NEW.map_basis IS OLD.map_basis)
  BEGIN SELECT RAISE(ABORT, 'provider_fixture_map is append-only (only revocation is allowed)'); END;
CREATE TRIGGER provider_fixture_map_no_delete BEFORE DELETE ON provider_fixture_map
  BEGIN SELECT RAISE(ABORT, 'provider_fixture_map is append-only'); END;

INSERT INTO schema_event VALUES (8, CAST(unixepoch('subsec') * 1000 AS INTEGER));

PRAGMA user_version = 8;
