"""Raw-first collector for api-football and OddsPapi (Pinnacle history).

Design rules (DESIGN v0.4 §5):
- Every HTTP response is archived as a self-describing envelope and indexed
  in raw_response BEFORE any parsing. Parsing is separate and rebuildable.
- API keys are never written to the archive or the database.
- OddsPapi billable calls pass a quota guard that checks /v4/account first.
- Every job runs inside a capture_slot so a run that never happens is visible.
"""
