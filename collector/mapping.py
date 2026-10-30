"""Cross-provider fixture mapping (requirement 3).

Workflow, with a human in the loop for the exact team allowlist:
  1. propose_teams(): for OddsPapi candidates whose participant ids are not yet
     in provider_team_map, find api-football fixtures in the SAME configured
     league whose kickoff agrees to within 15 minutes, and write a CSV of
     proposed participant-id -> team-id pairs with the evidence (both names,
     how many fixtures support the pair, and any conflicting pairs).
  2. You review the CSV, delete wrong rows, commit it to git.
  3. load_teams(csv, commit): loads the approved pairs into provider_team_map.
  4. apply(): maps every candidate whose two participant ids are allowlisted to
     the api-football fixture with exactly those home/away teams and a kickoff
     within 15 minutes. The schema re-checks all of this on insert.
  5. audit_kickoffs(): re-checks live mappings against the latest kickoffs and
     records disagreements in mapping_audit (the measured mis-join rate).
"""
from __future__ import annotations

import csv
import re
import sqlite3
from collections import defaultdict
from pathlib import Path

from .config import Config
from .store import now_ms

TOL_MS = 15 * 60_000


def _tournament_league(cfg: Config) -> dict[str, list[tuple[int, int]]]:
    """OddsPapi tournament id -> every configured (league, season) it covers (a tournament id may span seasons)."""
    out: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for l in cfg.leagues:
        if l.oddspapi_tournament_id:
            out[str(l.oddspapi_tournament_id)].append((l.league_id, l.season))
    return dict(out)


def _latest_candidates(con: sqlite3.Connection, provider: str):
    return con.execute(
        "SELECT provider_fixture_id, tournament_key, home_team_key, away_team_key, home_team_name, away_team_name,"
        " start_ms FROM (SELECT c.*, row_number() OVER (PARTITION BY provider_fixture_id ORDER BY raw_id DESC) rn"
        " FROM provider_fixture_candidate c WHERE provider=?) WHERE rn=1", (provider,)).fetchall()


def _af_matches(con, leagues, start_ms):
    rows = []
    for league_id, season in leagues:
        rows += con.execute(
            "SELECT f.fixture_id, f.home_team_id, f.away_team_id, th.name, ta.name, l.kickoff_ms"
            " FROM fixture f JOIN v_fixture_latest l USING (fixture_id)"
            " JOIN team th ON th.team_id = f.home_team_id JOIN team ta ON ta.team_id = f.away_team_id"
            " WHERE f.league_id=? AND f.season=? AND abs(l.kickoff_ms - ?) <= ?",
            (league_id, season, start_ms, TOL_MS)).fetchall()
    return rows


# OddsPapi lists undecided ties with placeholder participants ("Winner Match 2"). They
# are never real teams, so they are never proposed and the loader refuses them.
PLACEHOLDER = re.compile(
    r"^\s*(winner|loser|runner[- ]?up)s?\s+(of\s+)?(match|game|tie|group|semi|quarter|qf|sf|r\d|\d)"
    r"|\b(tbd|tba|to be (decided|announced))\b|^\s*(match|game|team)\s+\d+\s*$|\bplaceholder\b", re.I)
DOMINANCE = 5        # accept a row with conflicts only if its support is >= 5 x the conflicting support


def is_placeholder(name: str | None) -> bool:
    return bool(name) and bool(PLACEHOLDER.search(name))


def propose_teams(cfg: Config, con: sqlite3.Connection, out: Path, provider: str = "oddspapi") -> dict:
    """Propose provider participant -> api-football team pairs, for review.

    Evidence, per candidate fixture in a configured league:
      kickoff:  exactly one api-football fixture kicks off within 15 minutes;
      opponent: several do (simultaneous kickoffs, as on a UEFA matchday), but one
                participant is already APPROVED (loaded from a reviewed commit), and
                exactly one of those fixtures has that team on the same side. That
                pins the fixture, so the other participant is proposed as the other
                side. Only approved pairs propagate, never proposals from this run:
                each round of evidence rests on reviewed mappings. Load, then
                propose again, to take the next step.
    Names are evidence for the reviewer only; nothing is matched by name.

    Every row gets a `decision`: 'accept', or 'REVIEW: <why>'. The loader takes only
    rows marked accept; the reviewer deletes wrong rows and resolves REVIEW rows.
      accept: no conflicting team, or support >= DOMINANCE x all conflicting support;
      REVIEW: weaker conflicts, a team already approved for another key, or a team
              proposed for two keys in this file.
    """
    t2l = _tournament_league(cfg)
    approved = dict(con.execute("SELECT provider_team_key, team_id FROM provider_team_map WHERE provider=?",
                                (provider,)))
    approved_key_of = {t: k for k, t in approved.items()}
    support: dict[tuple[str, int], dict[str, set]] = defaultdict(lambda: {"kickoff": set(), "opponent": set()})
    names: dict[str, str] = {}
    placeholders: dict[str, str] = {}
    for pid, tkey, hk, ak, hn, an, start in _latest_candidates(con, provider):
        league = t2l.get(tkey)
        if league is None:
            continue
        names[hk], names[ak] = hn, an
        for k, nm in ((hk, hn), (ak, an)):
            if is_placeholder(nm):
                placeholders[k] = nm
        matches = _af_matches(con, league, start)
        if len(matches) == 1:
            _, hid, aid, _, _, _ = matches[0]
            support[(hk, hid)]["kickoff"].add(pid)
            support[(ak, aid)]["kickoff"].add(pid)
        elif len(matches) > 1:
            if hk in approved and ak not in approved:
                pin = [m for m in matches if m[1] == approved[hk]]
                if len(pin) == 1:
                    support[(ak, pin[0][2])]["opponent"].add(pid)
            elif ak in approved and hk not in approved:
                pin = [m for m in matches if m[2] == approved[ak]]
                if len(pin) == 1:
                    support[(hk, pin[0][1])]["opponent"].add(pid)
    by_key: dict[str, list] = defaultdict(list)
    for (k, team), ev in support.items():
        if k in approved or k in placeholders:
            continue
        by_key[k].append((team, len(ev["kickoff"] | ev["opponent"]), len(ev["kickoff"]), len(ev["opponent"])))
    team_names = dict(con.execute("SELECT team_id, name FROM team").fetchall())
    best_of = {k: sorted(v, key=lambda x: (-x[1], x[0]))[0][0] for k, v in by_key.items()}
    claimed = defaultdict(list)
    for k, t in best_of.items():
        claimed[t].append(k)
    out.parent.mkdir(parents=True, exist_ok=True)
    stats = {"proposed_rows": 0, "accept": 0, "review": 0, "via_opponent_rows": 0,
             "placeholders_excluded": sorted(set(placeholders.values()))}
    with open(out, "w", newline="") as fh:
        w = csv.writer(fh, lineterminator="\n")      # plain newlines: grep-friendly for the reviewer
        w.writerow(["provider", "provider_team_key", "provider_team_name", "team_id", "af_team_name",
                    "supporting_fixtures", "via_kickoff", "via_opponent", "conflicts", "decision"])
        for k in sorted(by_key, key=lambda x: names.get(x, "")):
            opts = sorted(by_key[k], key=lambda x: (-x[1], x[0]))
            best, cnt, n_ko, n_opp = opts[0]
            others = opts[1:]
            conflicts = "; ".join(f"{team_names.get(t)}({c})" for t, c, _, _ in others)
            if best in approved_key_of:
                decision = f"REVIEW: team already approved for key {approved_key_of[best]}"
            elif len(claimed[best]) > 1:
                decision = "REVIEW: team also proposed for key " + ", ".join(x for x in claimed[best] if x != k)
            elif others and cnt < DOMINANCE * sum(c for _, c, _, _ in others):
                decision = f"REVIEW: conflicts not dominated ({cnt} vs {sum(c for _, c, _, _ in others)})"
            else:
                decision = "accept"
            w.writerow([provider, k, names.get(k, ""), best, team_names.get(best, ""), cnt, n_ko, n_opp,
                        conflicts, decision])
            stats["proposed_rows"] += 1
            stats["accept" if decision == "accept" else "review"] += 1
            stats["via_opponent_rows"] += n_opp > 0
    return stats


def load_teams_from_git(con: sqlite3.Connection, repo: Path, commit: str, path_in_repo: str) -> dict:
    """Load the reviewed CSV exactly as committed: the bytes come from git, not from a
    working copy that could differ from what the commit hash claims."""
    import subprocess
    if len(commit) != 40 or any(c not in "0123456789abcdef" for c in commit):
        raise ValueError("commit must be the full 40-character lower-case git hash")
    git = ["git", "-c", f"safe.directory={repo}", "-C", str(repo)]
    subprocess.run(git + ["cat-file", "-e", f"{commit}^{{commit}}"], check=True, capture_output=True)
    blob = subprocess.run(git + ["show", f"{commit}:{path_in_repo}"], check=True, capture_output=True).stdout
    return _load_rows(con, blob.decode("utf-8").splitlines(), commit)


def load_teams(con: sqlite3.Connection, path: Path, commit: str) -> dict:
    if len(commit) != 40:
        raise ValueError("commit must be the full 40-character git hash that contains the reviewed CSV")
    with open(path, newline="") as fh:
        return _load_rows(con, fh.read().splitlines(), commit)


def _load_rows(con: sqlite3.Connection, lines: list[str], commit: str) -> dict:
    """All or nothing. A row is loaded only if its decision is 'accept' (or, in a CSV
    from before the decision column, if it lists no conflicts). A row identical to an
    already-approved pair is skipped, so a cumulative CSV can be loaded again; a row
    that would CHANGE an approved pair is refused."""
    n = skipped = 0
    con.execute("BEGIN")
    try:
        for row in csv.DictReader(lines):
            key, team = row["provider_team_key"], int(row["team_id"])
            if is_placeholder(row.get("provider_team_name")):
                raise ValueError(f"row for {key} is a placeholder participant ({row['provider_team_name']!r}): delete it")
            if "decision" in row:
                if (row["decision"] or "").strip() != "accept":
                    raise ValueError(f"row for {key} is marked {row['decision']!r}: resolve it to 'accept' or delete "
                                     "the row before loading")
            elif row.get("conflicts"):
                raise ValueError(f"row for {key} still lists conflicts: resolve before loading")
            have = con.execute("SELECT team_id FROM provider_team_map WHERE provider=? AND provider_team_key=?",
                               (row["provider"], key)).fetchone()
            if have is not None:
                if have[0] != team:
                    raise ValueError(f"row for {key} would change an approved pair ({have[0]} -> {team})")
                skipped += 1
                continue
            con.execute("INSERT INTO provider_team_map VALUES (?,?,?,?,?)",
                        (row["provider"], key, row["provider_team_name"], team, commit))
            n += 1
    except BaseException:
        con.execute("ROLLBACK")
        raise
    con.execute("COMMIT")
    return {"loaded": n, "already_loaded": skipped}


def apply(cfg: Config, con: sqlite3.Connection, provider: str = "oddspapi") -> dict:
    t2l = _tournament_league(cfg)
    team = dict(con.execute("SELECT provider_team_key, team_id FROM provider_team_map WHERE provider=?", (provider,)))
    live = {r[0] for r in con.execute("SELECT provider_fixture_id FROM provider_fixture_map WHERE provider=?", (provider,))}
    stats = defaultdict(int)
    for pid, tkey, hk, ak, hn, an, start in _latest_candidates(con, provider):
        if pid in live:
            stats["already_mapped"] += 1
            continue
        league = t2l.get(tkey)
        if league is None:
            stats["untracked_tournament"] += 1
            continue
        if hk not in team or ak not in team:
            stats["team_not_allowlisted"] += 1
            continue
        matches = [m for m in _af_matches(con, league, start) if m[1] == team[hk] and m[2] == team[ak]]
        if len(matches) != 1:
            stats["no_unique_af_fixture" if not matches else "ambiguous"] += 1
            continue
        fid, _, _, hname, aname, ko = matches[0]
        evidence = (f"participants {hk}={hn!r}->{team[hk]} {hname!r}, {ak}={an!r}->{team[ak]} {aname!r};"
                    f" start {start} vs kickoff {ko} (diff {abs(start - ko) // 1000}s)")
        try:
            con.execute("INSERT INTO provider_fixture_map (provider, provider_fixture_id, fixture_id, home_team_key,"
                        " away_team_key, provider_start_ms, kickoff_at_map_ms, evidence, mapped_at_ms)"
                        " VALUES (?,?,?,?,?,?,?,?,?)", (provider, pid, fid, hk, ak, start, ko, evidence, now_ms()))
            stats["mapped"] += 1
        except sqlite3.IntegrityError as e:
            stats[f"refused: {str(e)[:80]}"] += 1
    return dict(stats)


def audit_kickoffs(con: sqlite3.Connection, provider: str = "oddspapi") -> dict:
    rows = con.execute(
        "SELECT m.provider_fixture_id, m.fixture_id, l.kickoff_ms,"
        " (SELECT c.start_ms FROM provider_fixture_candidate c WHERE c.provider=m.provider"
        "   AND c.provider_fixture_id=m.provider_fixture_id ORDER BY c.raw_id DESC LIMIT 1),"
        " (SELECT c.raw_id FROM provider_fixture_candidate c WHERE c.provider=m.provider"
        "   AND c.provider_fixture_id=m.provider_fixture_id ORDER BY c.raw_id DESC LIMIT 1)"
        " FROM provider_fixture_map m JOIN v_fixture_latest l ON l.fixture_id = m.fixture_id"
        " WHERE m.provider=? AND m.revoked=0 AND l.status_short IN ('FT','AET','PEN')"
        " AND NOT EXISTS (SELECT 1 FROM mapping_audit a WHERE a.provider=m.provider"
        "   AND a.provider_fixture_id=m.provider_fixture_id AND a.method='kickoff_recheck')", (provider,)).fetchall()
    out = defaultdict(int)
    for pid, fid, ko, start, raw_id in rows:
        agrees = int(abs(start - ko) <= TOL_MS)
        con.execute("INSERT INTO mapping_audit (provider, provider_fixture_id, method, agrees, detail, evidence_raw_id,"
                    " recorded_at_ms) VALUES (?,?,'kickoff_recheck',?,?,?,?)",
                    (provider, pid, agrees, f"final kickoff {ko} vs latest provider start {start}", raw_id, now_ms()))
        out["agree" if agrees else "disagree"] += 1
    return dict(out)


def manual_sample(con: sqlite3.Connection, out: Path, n: int = 30, seed: int = 20261023,
                  provider: str = "oddspapi") -> int:
    """Random sample of live mappings for a by-hand check; the seed is fixed so the sample is auditable."""
    rows = con.execute(
        "SELECT m.provider_fixture_id, m.fixture_id, m.evidence FROM provider_fixture_map m"
        " WHERE m.provider=? AND m.revoked=0 ORDER BY m.provider_fixture_id", (provider,)).fetchall()
    import random
    rnd = random.Random(seed)
    pick = rnd.sample(rows, min(n, len(rows)))
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["provider_fixture_id", "fixture_id", "evidence", "agrees(1/0)", "checked_against", "notes"])
        for r in pick:
            w.writerow([*r, "", "", ""])
    return len(pick)


# --- reverse direction: tracked api-football fixtures with no mapping (fix 5) ---------------

ALERT_SHARE = 0.10          # pre-registered: alert above 10% unmapped in the next 72 h ...
ALERT_MIN_FIXTURES = 5      # ... for a tournament with at least 5 upcoming fixtures
RELIST_SHARE = 0.10         # relist when >10% of upcoming fixtures have NO OddsPapi candidate at all


def unmapped_upcoming(cfg: Config, con: sqlite3.Connection, horizon_h: int = 72,
                      provider: str = "oddspapi", t: int | None = None) -> dict[str, dict]:
    """Per OddsPapi tournament: api-football fixtures kicking off within horizon_h, and
    why any of them lacks a mapping. 'candidate_unmapped' means OddsPapi lists a fixture
    at that time but it did not map (usually a team not yet in the allowlist: relisting
    won't help, review will). 'no_candidate' means OddsPapi has not listed it: relisting may."""
    t = now_ms() if t is None else t
    out: dict[str, dict] = {}
    for tkey, leagues in _tournament_league(cfg).items():
        n = mapped = cand_unmapped = no_cand = 0
        for league_id, season in leagues:
            rows = con.execute(
                "SELECT f.fixture_id, l.kickoff_ms FROM fixture f JOIN v_fixture_latest l USING (fixture_id)"
                " WHERE f.league_id=? AND f.season=? AND l.status_short IN ('NS','TBD') AND l.kickoff_ms BETWEEN ? AND ?",
                (league_id, season, t, t + horizon_h * 3_600_000)).fetchall()
            for fid, ko in rows:
                n += 1
                if con.execute("SELECT 1 FROM provider_fixture_map WHERE provider=? AND fixture_id=? AND revoked=0",
                               (provider, fid)).fetchone():
                    mapped += 1
                elif con.execute("SELECT 1 FROM provider_fixture_candidate WHERE provider=? AND tournament_key=?"
                                 " AND abs(start_ms - ?) <= ?", (provider, tkey, ko, TOL_MS)).fetchone():
                    cand_unmapped += 1
                else:
                    no_cand += 1
        share = (n - mapped) / n if n else 0.0
        out[tkey] = {"upcoming": n, "mapped": mapped, "candidate_unmapped": cand_unmapped, "no_candidate": no_cand,
                     "unmapped_share": round(share, 3),
                     "alert": n >= ALERT_MIN_FIXTURES and share > ALERT_SHARE,
                     "needs_relisting": n > 0 and no_cand / n > RELIST_SHARE}
    return out
