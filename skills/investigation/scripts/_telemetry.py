"""Shared telemetry snapshot format (pinned in references/telemetry.md). Stdlib only.

A capture lives at <study>/telemetry/<capture-id>/:

  capture.json        CAPTURE_VERSION; source, window, queries, empty_queries,
                      files {relpath: sha256}, moved {relpath: {sha256, path}}
  prom/<name>.json    the raw Prometheus query_range response, byte-for-byte
  metrics.jsonl       one OTel resourceMetrics record per (query x series)
  summary.json        per-query reductions (see summarize_query_range)

When the capture exceeds the size guard, prom/ and metrics.jsonl move to the
runs dir; capture.json["moved"] records where and their sha256, and
summary.json stays in the study so claims still have something to cite.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

CAPTURE_VERSION = 1
SUMMARY_VERSION = 1
SIZE_GUARD_BYTES = 50 * 1024 * 1024        # overridable via $INVESTIGATION_SIZE_GUARD_BYTES
MAX_POINTS_PER_SERIES = 11_000             # Prometheus query_range limit


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _finite(values: list) -> list[float]:
    """Finite float values only: non-numeric and non-finite (NaN/Inf) samples
    are dropped. Ints beyond float range raise OverflowError from float(v)
    (json.loads parses a >308-digit JSON number back as a Python int), so they
    are dropped here too — the same fail-closed outcome as the string form,
    which converts to inf and fails the isfinite check."""
    out = []
    for _ts, v in values:
        try:
            x = float(v)
        except (TypeError, ValueError, OverflowError):
            continue
        if math.isfinite(x):
            out.append(x)
    return out


def _stats(xs: list[float], n_series: int) -> dict:
    if not xs:
        return {"mean": None, "p50": None, "max": None, "min": None,
                "n_points": 0, "n_series": n_series}
    s = sorted(xs)
    n = len(s)
    p50 = s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2
    return {"mean": sum(s) / n, "p50": p50, "max": s[-1], "min": s[0],
            "n_points": n, "n_series": n_series}


def summarize_query_range(resp: dict) -> dict:
    """Reduce one raw query_range response to {overall: stats, series: [...]}.

    stats = {mean, p50, max, min, n_points, n_series}; NaN/Inf samples are
    dropped (histogram_quantile emits NaN on empty windows). An empty result
    gives n_series 0 and null stats — empty is recorded, never an error.
    """
    result = (resp.get("data") or {}).get("result") or []
    series, all_points = [], []
    for s in result:
        xs = _finite(s.get("values") or [])
        all_points.extend(xs)
        st = _stats(xs, 1)
        st["labels"] = dict(s.get("metric") or {})
        series.append(st)
    return {"overall": _stats(all_points, len(result)), "series": series}


def prom_path(capture_dir: Path, name: str) -> Path:
    """Where prom/<name>.json actually is: in the study, or moved to the runs dir."""
    rel = f"prom/{name}.json"
    local = capture_dir / rel
    if local.is_file():
        return local
    cap = json.loads((capture_dir / "capture.json").read_text())
    moved = (cap.get("moved") or {}).get(rel)
    if moved:
        return Path(moved["path"])
    return local
