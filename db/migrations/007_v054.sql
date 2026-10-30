-- v0.5.4: a settled "no history" outcome for OddsPapi history requests.
--
-- Until now a history request was either complete (a usable 200) or failed, and one
-- failed request failed the whole slot. A 404 from /v4/historical-odds is a definite
-- answer ("no history for this fixture"), not a transport failure: it must neither
-- page healthchecks nor be retried hourly. It is recorded here, append-only, and only
-- when the raw layer itself shows a 404 and no usable response for that request.
-- Transport failures (429 after back-off, 5xx, network) still fail the slot.

CREATE TABLE request_outcome (
  request_id     INTEGER PRIMARY KEY REFERENCES api_request(request_id),
  outcome        TEXT    NOT NULL CHECK (outcome IN ('no_history')),
  http_status    INTEGER NOT NULL CHECK (http_status = 404),
  decided_at_ms  INTEGER NOT NULL
) STRICT;

CREATE TRIGGER request_outcome_guard
BEFORE INSERT ON request_outcome
BEGIN
  SELECT RAISE(ABORT, 'request_outcome: only OddsPapi /v4/historical-odds requests can be settled as no_history')
   WHERE (SELECT provider || ' ' || endpoint FROM api_request WHERE request_id = NEW.request_id)
         IS NOT 'oddspapi /v4/historical-odds';
  SELECT RAISE(ABORT, 'request_outcome: the request must have failed')
   WHERE (SELECT status FROM api_request WHERE request_id = NEW.request_id) IS NOT 'failed';
  SELECT RAISE(ABORT, 'request_outcome: no_history needs a recorded 404 and no usable response')
   WHERE NOT EXISTS (SELECT 1 FROM raw_response WHERE request_id = NEW.request_id AND http_status = 404)
      OR EXISTS (SELECT 1 FROM raw_response WHERE request_id = NEW.request_id AND usable = 1);
END;
CREATE TRIGGER request_outcome_no_update BEFORE UPDATE ON request_outcome
BEGIN SELECT RAISE(ABORT, 'request_outcome is append-only'); END;
CREATE TRIGGER request_outcome_no_delete BEFORE DELETE ON request_outcome
BEGIN SELECT RAISE(ABORT, 'request_outcome is append-only'); END;

-- A slot completes when every request it made is complete OR settled as no_history.
DROP TRIGGER capture_slot_complete_requires_all_requests;
CREATE TRIGGER capture_slot_complete_requires_all_requests
BEFORE UPDATE OF status ON capture_slot
WHEN NEW.status = 'complete'
BEGIN
  SELECT RAISE(ABORT, 'capture_slot: completed requests != expected_requests')
   WHERE (SELECT count(*) FROM api_request a
           WHERE a.slot_id = NEW.slot_id
             AND (a.status = 'complete'
                  OR EXISTS (SELECT 1 FROM request_outcome o WHERE o.request_id = a.request_id))) <> NEW.expected_requests;
  SELECT RAISE(ABORT, 'capture_slot: has open or failed requests')
   WHERE EXISTS (SELECT 1 FROM api_request a
                  WHERE a.slot_id = NEW.slot_id AND a.status <> 'complete'
                    AND NOT EXISTS (SELECT 1 FROM request_outcome o WHERE o.request_id = a.request_id));
END;

INSERT INTO schema_event VALUES (7, CAST(unixepoch('subsec') * 1000 AS INTEGER));

PRAGMA user_version = 7;
