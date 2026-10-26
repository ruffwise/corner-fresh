"""Configuration: non-secret settings from config.toml, secrets from the environment."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


# Jobs driven by a systemd timer and the slot ledger. Which of them run is decided
# by `active_jobs` in the committed config: the planner plans slots only for those,
# the watchdog checks only those, and release.sh enables exactly their timers.
SCHEDULED_JOBS = ("af_fixtures", "af_odds", "af_results", "op_account", "op_fixtures", "op_history")


@dataclass(frozen=True)
class League:
    league_id: int
    season: int
    oddspapi_tournament_id: int | None = None   # filled after the day-1 catalog review


@dataclass(frozen=True)
class Config:
    db_path: Path
    archive_dir: Path
    review_dir: Path
    leagues: tuple[League, ...]
    repo_dir: Path = Path("/opt/corners/repo")
    af_base: str = "https://v3.football.api-sports.io"
    af_key: str = ""
    af_odds_horizon_days: int = 14
    af_min_interval_s: float = 0.25
    op_base: str = "https://api.oddspapi.io"
    op_key: str = ""
    op_bookmakers: str = "pinnacle"
    op_monthly_billable_budget: int = 20      # our own cap, far below the provider limit
    op_quota_reserve: int = 50                # never let the provider count come within this of the limit
    op_fixtures_refresh_days: int = 28
    op_fixtures_from: str | None = None       # e.g. "2026-01-01T00:00:00Z" for the one-off backfill listing
    op_history_delay_h: float = 3.0           # pull history this long after scheduled kickoff
    op_history_max_per_run: int = 60          # ~5 minutes at the 5 s cooldown
    healthchecks: dict[str, str] = field(default_factory=dict)
    active_jobs: tuple[str, ...] = ()


def load(path: str | Path) -> Config:
    path = Path(path)
    raw = tomllib.loads(path.read_text())
    base = path.parent
    leagues = tuple(
        League(int(x["league_id"]), int(x["season"]),
               int(x["oddspapi_tournament_id"]) if x.get("oddspapi_tournament_id") else None)
        for x in raw.get("leagues", []))
    if not leagues:
        raise ValueError("config: no leagues configured")
    ids = [(l.league_id, l.season) for l in leagues]
    if len(ids) != len(set(ids)):
        raise ValueError("config: duplicate (league_id, season)")
    if "active_jobs" not in raw:
        raise ValueError("config: active_jobs is missing; list the scheduled jobs whose timers should run "
                         f"(any of {', '.join(SCHEDULED_JOBS)})")
    active = tuple(raw["active_jobs"])
    unknown = sorted(set(active) - set(SCHEDULED_JOBS))
    if unknown or len(active) != len(set(active)):
        raise ValueError(f"config: active_jobs has unknown or repeated entries {unknown or active}")
    af = raw.get("api_football", {})
    op = raw.get("oddspapi", {})
    hc = {k[3:].lower(): v for k, v in os.environ.items() if k.startswith("HC_") and v}
    return Config(
        db_path=(base / raw.get("db_path", "data/corners.db")).resolve(),
        archive_dir=(base / raw.get("archive_dir", "data/archive")).resolve(),
        review_dir=(base / raw.get("review_dir", "data/review")).resolve(),
        leagues=leagues,
        repo_dir=Path(raw.get("repo_dir", "/opt/corners/repo")),
        af_base=af.get("base", Config.af_base),
        af_key=os.environ.get("AF_KEY", ""),
        af_odds_horizon_days=int(af.get("odds_horizon_days", 14)),
        af_min_interval_s=float(af.get("min_interval_s", 0.25)),
        op_base=op.get("base", Config.op_base),
        op_key=os.environ.get("OP_KEY", ""),
        op_bookmakers=op.get("bookmakers", "pinnacle"),
        op_monthly_billable_budget=int(op.get("monthly_billable_budget", 20)),
        op_quota_reserve=int(op.get("quota_reserve", 50)),
        op_fixtures_refresh_days=int(op.get("fixtures_refresh_days", 28)),
        op_fixtures_from=op.get("fixtures_from"),
        op_history_delay_h=float(op.get("history_delay_h", 3.0)),
        op_history_max_per_run=int(op.get("history_max_per_run", 60)),
        healthchecks=hc,
        active_jobs=tuple(j for j in SCHEDULED_JOBS if j in active),
    )
