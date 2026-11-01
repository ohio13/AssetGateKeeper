"""Historical trends across multi-year development cycles.

Three questions a studio actually asks, answered from the verdict history:

    compliance_trend()   are we getting better at shipping to budget?
    polygon_growth()     is the content getting more expensive over time?
    error_frequency()    which mistakes do we keep repeating?

Design notes
    * **Aggregation is pushed into SQL where the two engines agree** (COUNT/CASE
      BY DATE_TRUNC) and done in Python where they do not (parsing "12,345 tris"
      out of a metric string). A trend that is fast on PostgreSQL and correct on
      SQLite is worth more than clever SQL that is silently wrong on one of them.
    * **Buckets are explicit strings**, never dates with timezone surprises:
      ``2026-10-31``, ``2026-10``, ``2026-W44``. They sort lexicographically, so
      a chart does not need to know about timezones to put them in order.
    * **Windows are half-open** ``[since, until)`` and always inclusive of the
      named period start, because "this month" must mean the same thing to the
      dashboard and to the person reading it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

BUCKETS = ("hour", "day", "week", "month", "year")

_SQLITE_FORMATS = {
    "hour": "%Y-%m-%dT%H",
    "day": "%Y-%m-%d",
    "week": "%Y-W%W",
    "month": "%Y-%m",
    "year": "%Y",
}
_POSTGRES_FORMATS = {
    "hour": "YYYY-MM-DD\"T\"HH24",
    "day": "YYYY-MM-DD",
    "week": "IYYY-\"W\"IW",
    "month": "YYYY-MM",
    "year": "YYYY",
}

_TRIANGLES_RE = re.compile(r"([\d][\d,]*)\s*tris", re.IGNORECASE)
_NUMBER_RE = re.compile(r"\d[\d,\.]*")


class AnalyticsError(Exception):
    """Asked for a trend the stored data cannot answer."""


# --------------------------------------------------------------------------- #
# Time windows
# --------------------------------------------------------------------------- #

_UNIT_SECONDS = {"h": 3600, "d": 86400, "w": 7 * 86400, "m": 30 * 86400,
                 "y": 365 * 86400}


def parse_since(spec: str | None, now: Optional[datetime] = None) -> Optional[str]:
    """``"90d"``, ``"18m"`` or ``"2024-01-01"`` -> an ISO timestamp string.

    Relative windows are deliberately coarse units: a trend chart does not need
    leap-second precision, and "6 months" meaning 180 days is easier to explain
    to a producer than a calendar-month iterator.
    """
    if not spec:
        return None
    text = str(spec).strip().lower()
    now = now or datetime.now(timezone.utc)
    if text and text[-1] in _UNIT_SECONDS and text[:-1].isdigit():
        delta = timedelta(seconds=int(text[:-1]) * _UNIT_SECONDS[text[-1]])
        return (now - delta).isoformat(timespec="seconds")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise AnalyticsError(
            f"cannot read '{spec}' as a window; use 90d, 18m or 2024-01-01") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.isoformat(timespec="seconds")


def bucket_expression(dialect_name: str, column: str, bucket: str) -> str:
    """The SQL expression that renders a timestamp as a sortable bucket label."""
    if bucket not in BUCKETS:
        raise AnalyticsError(f"unknown bucket '{bucket}' (use {', '.join(BUCKETS)})")
    if dialect_name == "postgres":
        fmt = _POSTGRES_FORMATS[bucket]
        return f"to_char({column}::timestamptz, '{fmt}')"
    fmt = _SQLITE_FORMATS[bucket]
    return f"strftime('{fmt}', {column})"


# --------------------------------------------------------------------------- #
# Results
# --------------------------------------------------------------------------- #

@dataclass
class TrendPoint:
    bucket: str
    total: int = 0
    passed: int = 0
    healed: int = 0
    failed: int = 0

    @property
    def compliance(self) -> float:
        """Share of assets that left the gate acceptable.

        ``passed`` already counts healed rows (a healed asset passed, it was
        just rewritten on the way in), so healing must not be added again -
        that double count is how a dashboard reports 140% compliance.
        """
        if not self.total:
            return 0.0
        return round(self.passed / self.total, 4)

    def as_dict(self) -> dict[str, Any]:
        return {"bucket": self.bucket, "total": self.total, "passed": self.passed,
                "healed": self.healed, "failed": self.failed,
                "compliance": self.compliance}


@dataclass
class GrowthPoint:
    bucket: str
    assets: int = 0
    average_triangles: int = 0
    peak_triangles: int = 0
    total_triangles: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {"bucket": self.bucket, "assets": self.assets,
                "average_triangles": self.average_triangles,
                "peak_triangles": self.peak_triangles,
                "total_triangles": self.total_triangles}


@dataclass
class ErrorCluster:
    signature: str
    example: str
    count: int
    first_seen: str
    last_seen: str

    def as_dict(self) -> dict[str, Any]:
        return {"signature": self.signature, "example": self.example,
                "count": self.count, "first_seen": self.first_seen,
                "last_seen": self.last_seen}


@dataclass
class TrendReport:
    points: list[Any] = field(default_factory=list)
    window: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"window": self.window,
                "points": [p.as_dict() if hasattr(p, "as_dict") else p
                           for p in self.points]}

    @property
    def delta(self) -> float:
        """Change in compliance between the first and last bucket, as a ratio."""
        values = [p.compliance for p in self.points if getattr(p, "total", 0)]
        if len(values) < 2:
            return 0.0
        return round(values[-1] - values[0], 4)


# --------------------------------------------------------------------------- #
# Queries
# --------------------------------------------------------------------------- #

def _where(since: Optional[str], until: Optional[str], project_id: Optional[int],
           alias: str = "") -> tuple[str, list[Any]]:
    prefix = f"{alias}." if alias else ""
    clauses, params = [], []
    if since:
        clauses.append(f"{prefix}timestamp >= ?")
        params.append(since)
    if until:
        clauses.append(f"{prefix}timestamp < ?")
        params.append(until)
    if project_id is not None:
        clauses.append(f"{prefix}project_id = ?")
        params.append(project_id)
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", params


def compliance_trend(store: Any, bucket: str = "day", since: str | None = None,
                     until: str | None = None, project_id: int | None = None) -> TrendReport:
    """Pass rate per bucket, with healed counted as acceptable but visible."""
    since_iso, until_iso = parse_since(since), parse_since(until)
    label = bucket_expression(store.dialect.name, "timestamp", bucket)
    where, params = _where(since_iso, until_iso, project_id)
    sql = (f"SELECT {label} AS bucket, COUNT(*) AS total, "
           "SUM(CASE WHEN passed = 1 THEN 1 ELSE 0 END) AS passed, "
           "SUM(CASE WHEN status = 'HEALED' THEN 1 ELSE 0 END) AS healed "
           f"FROM verdicts{where} GROUP BY bucket ORDER BY bucket")
    rows = store.query(sql, tuple(params)) if params else store.query(sql)
    points = []
    for row in rows:
        total = int(row["total"] or 0)
        passed = int(_get(row, "passed", 0) or 0)
        healed = int(_get(row, "healed", 0) or 0)
        points.append(TrendPoint(bucket=str(_get(row, "bucket", "")), total=total,
                                 passed=passed, healed=healed,
                                 failed=max(0, total - passed)))
    report = TrendReport(points=points,
                         window={"since": since_iso, "until": until_iso,
                                 "bucket": bucket, "project_id": project_id})
    return report


def _get(row: Any, key: str, default: Any = None) -> Any:
    """Rows are sqlite3.Row, dicts or tuples depending on the engine and driver."""
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return default


def triangles_from_metric(text: str | None) -> Optional[int]:
    """Pull a triangle count out of the gate's human-readable metric string.

    The gate writes ``"12,345 tris / 3 mat"``. Parsing it here means history that
    predates the metric table still produces a growth chart - and if the text is
    unreadable the point is skipped rather than counted as zero.
    """
    if not text:
        return None
    match = _TRIANGLES_RE.search(text)
    if match is None:
        return None
    try:
        return int(match.group(1).replace(",", ""))
    except ValueError:
        return None


def polygon_growth(store: Any, bucket: str = "month", since: str | None = None,
                   until: str | None = None, project_id: int | None = None,
                   meshes_only: bool = True) -> TrendReport:
    """Average and peak triangle count per bucket.

    A rising average with a flat asset count is a content problem; a flat average
    with a rising count is a scope problem. Both matter, so both are returned.
    """
    since_iso, until_iso = parse_since(since), parse_since(until)
    where, params = _where(since_iso, until_iso, project_id)
    sql = f"SELECT timestamp, metric FROM verdicts{where}"
    rows = store.query(sql, tuple(params)) if params else store.query(sql)
    totals: dict[str, list[int]] = {}
    # Reuse the engine's own bucketing on already-fetched timestamps by asking it
    # once per distinct timestamp would be absurd; a deterministic Python bucketer
    # keeps this readable and engine-independent.
    for row in rows:
        count = triangles_from_metric(_get(row, "metric", ""))
        if count is None:
            continue
        if meshes_only and count <= 0:
            continue
        stamp = str(_get(row, "timestamp", ""))
        totals.setdefault(_python_bucket(stamp, bucket), []).append(count)
    points = []
    for label_value in sorted(totals):
        values = totals[label_value]
        points.append(GrowthPoint(bucket=label_value, assets=len(values),
                                  average_triangles=int(sum(values) / len(values)),
                                  peak_triangles=max(values),
                                  total_triangles=sum(values)))
    if not points:
        # Documented failure mode: an empty chart must say why, not just be empty.
        raise AnalyticsError(
            "no triangle counts in this window: the verdicts have no 'tris' metric "
            "(or the window is empty)")
    return TrendReport(points=points,
                       window={"since": since_iso, "until": until_iso,
                               "bucket": bucket, "project_id": project_id})


def _python_bucket(stamp: str, bucket: str) -> str:
    try:
        when = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return stamp[:7] if bucket == "month" else stamp[:10]
    if bucket == "hour":
        return when.strftime("%Y-%m-%dT%H")
    if bucket == "day":
        return when.strftime("%Y-%m-%d")
    if bucket == "week":
        iso = when.isocalendar()
        return f"{iso.year}-W{iso.week:02d}"
    if bucket == "month":
        return when.strftime("%Y-%m")
    return when.strftime("%Y")


def signature_of(problem: str) -> str:
    """Fold a problem message into a stable cluster key.

    ``"72,500 triangles exceeds the 50,000 budget (+22,500)"`` and the same line
    for a different asset become one signature, which is what "which mistake do
    we keep making" actually means.
    """
    text = _NUMBER_RE.sub("#", problem.strip().lower())
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"#[^ ]*", "#", text)
    return text[:180]


def error_frequency(store: Any, since: str | None = None, until: str | None = None,
                    project_id: int | None = None, limit: int = 20) -> list[ErrorCluster]:
    """The recurring violations, newest window first, clustered by signature."""
    since_iso, until_iso = parse_since(since), parse_since(until)
    where, params = _where(since_iso, until_iso, project_id, alias="v")
    sql = ("SELECT p.message AS message, v.timestamp AS timestamp "
           "FROM problems p JOIN verdicts v ON v.id = p.verdict_id"
           f"{where} ORDER BY v.timestamp DESC")
    rows = store.query(sql, tuple(params)) if params else store.query(sql)
    clusters: dict[str, ErrorCluster] = {}
    for row in rows:
        message = str(_get(row, "message", "") or "").strip()
        if not message:
            continue
        key = signature_of(message)
        stamp = str(_get(row, "timestamp", "") or "")
        existing = clusters.get(key)
        if existing is None:
            clusters[key] = ErrorCluster(signature=key, example=message, count=1,
                                         first_seen=stamp, last_seen=stamp)
        else:
            existing.count += 1
            existing.first_seen = min(existing.first_seen, stamp)
            existing.last_seen = max(existing.last_seen, stamp)
    ordered = sorted(clusters.values(), key=lambda c: (-c.count, c.signature))
    return ordered[:max(1, limit)]


def compliance_summary(store: Any, since: str | None = None, until: str | None = None,
                       project_id: int | None = None) -> dict[str, Any]:
    """One number for a wallboard, plus the denominator it came from."""
    report = compliance_trend(store, bucket="month", since=since, until=until,
                              project_id=project_id)
    total = sum(point.total for point in report.points)
    passed = sum(point.passed for point in report.points)
    return {
        "window": report.window,
        "total": total,
        "acceptable": passed,
        "failed": total - passed,
        "compliance": round(passed / total, 4) if total else None,
        "trend": report.delta,
        "buckets": len(report.points),
    }


def render_ascii(report: TrendReport, width: int = 40) -> str:
    """A sparkline for the terminal: ``agk-enterprise trends`` has no browser."""
    points = [p for p in report.points if hasattr(p, "compliance")]
    if not points:
        return "(no data)"
    peak = max(p.compliance for p in points) or 1.0
    lines = []
    for point in points:
        filled = int(round(point.compliance / peak * width))
        lines.append(f"{point.bucket:<12} {'#' * filled:<{width}} "
                     f"{point.compliance * 100:5.1f}%  n={point.total}")
    return "\n".join(lines)


def iter_buckets(start: datetime, end: datetime, bucket: str) -> Iterable[str]:
    """Calendar buckets for callers that need them without a database."""
    if bucket not in BUCKETS:
        raise AnalyticsError(f"unknown bucket '{bucket}'")
    if bucket == "hour":
        step = timedelta(hours=1)
    elif bucket == "day":
        step = timedelta(days=1)
    elif bucket == "week":
        step = timedelta(days=7)
    elif bucket == "month":
        step = timedelta(days=31)
    else:
        step = timedelta(days=365)
    cursor = start
    while cursor < end:
        yield _python_bucket(cursor.isoformat(timespec="seconds"), bucket)
        cursor += step
