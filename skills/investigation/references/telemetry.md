# Telemetry: frozen Prometheus snapshots

Metrics only; no OTel traces are available. Every capture becomes a **frozen
snapshot** inside the study. Never cite a live endpoint as evidence: the answer
contract needs evidence that someone can re-check later.

A capture reads from the cluster and never writes to it, but it still touches an external system.
So it gets **its own go** from the owner, separate from approving the plan.
This is a **legacy gate, not runtime-enforced**: captures don't go through
`scripts/runtime.py` yet, so the go in chat is the only gate.

## Sources

Pick one per study in `plan.md`.

| Source | How |
|---|---|
| `port-forward` | `--port-forward CTX/NS/SVC[:PORT]`. The script picks a free local port, runs `kubectl --context CTX -n NS port-forward svc/SVC LOCAL:PORT` (remote port defaults to 9090), waits until the port accepts connections (up to `$INVESTIGATION_PF_TIMEOUT`, default 20 s), queries `127.0.0.1`, and terminates the process group in `finally` (SIGTERM, then SIGKILL). |
| `url` | `--url URL [--token-env VAR]`: a reachable Prometheus or Thanos base URL. The bearer token is read **only** from the env var whose *name* is given. It is never printed, never written, and redacted from every error message. A URL that carries credentials, a query string or a fragment is refused. A plain-http URL to a non-loopback host is refused (the token would travel in cleartext); loopback http is fine, and `--allow-insecure-http` acknowledges the risk and overrides the refusal. |
| `import` | No script. Copy an existing directory in the format `signal-pipeline` accepts into `telemetry/<id>/` as-is: raw files (`metrics.jsonl`, `vllm_server.log`, …), a pre-cooked `01_signals.json`, or sim2real's `metrics/raw/*_metrics.log` dumps. Write a `capture.json` by hand with `source.kind: "import"` and the origin path, and fill `files` with sha256 of what you copied. |

## Command

```
scripts/capture_prom.py --study investigations/<slug> --capture-id <id>
    (--url URL [--token-env VAR] [--allow-insecure-http] | --port-forward CTX/NS/SVC[:PORT])
    --start <rfc3339> --end <rfc3339> [--step 30s]
    [--queries FILE] [--extra-queries FILE]
```

- `--start` / `--end` are required and must be RFC 3339 with a timezone
  (`2026-11-05T10:00:00Z`). There is no default window.
- Queries are flat YAML `{name: promql}` mappings. They are merged in this order,
  with later entries winning on a name clash:
  1. `--queries`, which defaults to `assets/promql/starter.yaml` next to the scripts;
  2. `promql_extra` from the repo config, when the study is inside a git repo;
  3. `--extra-queries`.

  Names must match `[A-Za-z0-9_][A-Za-z0-9_.-]*`, because each one becomes a file name.
- `--capture-id` must be new. Captures are frozen, so the script refuses to write into an existing one.
- Exit codes:
  - `0`: captured. Some queries may be empty or failed; those are recorded.
  - `1`: every query failed (nothing is written), or the port-forward failed.
  - `2`: bad arguments, including the step limit; also the cleartext-http
    refusal (`--url` with plain `http` to a non-loopback host without
    `--allow-insecure-http`).

Each `query_range` request gets 60 s (`$INVESTIGATION_PROM_TIMEOUT` to
override; a slow Thanos frontend may need more). The port-forward readiness
wait is 20 s (`$INVESTIGATION_PF_TIMEOUT`), and the per-capture size guard
is 50 MiB (`$INVESTIGATION_SIZE_GUARD_BYTES`, see "Size guard" below).

## Choosing the window

The window depends on the question. The skill recommends one in `plan.md`, and
the owner confirms or changes it at the capture go.

| Question shape | Recommended window |
|---|---|
| Names an incident, deploy, or benchmark run | that event ± a margin (e.g. 15 min before → 15 min after); ask for the timestamps if they aren't given |
| "Before vs after" a change | two captures of equal length, one on each side of the change |
| Steady-state behavior ("what's our prefix hit rate") | the most recent 24 h, or 7 d if daily cycles matter |
| "Right now" | the last 1 h |

Always write the window in `plan.md` as absolute RFC 3339 times, even when it
was recommended as "the last 24 h", so the capture can be reproduced.
`capture.json.window` records the window that was actually used.

## The step rule

Prometheus refuses a `query_range` that returns more than 11,000 points per
series. The script checks `(end - start) / step <= 11000` before it sends any
query. If the check fails, the script exits 2 and **suggests a step** (the
smallest of 1s, 5s, 10s, 15s, 30s, 1m, 2m, 5m, 10m, 15m, 30m, 1h, … that fits).
It never clamps the step silently. As a guide, 30 s works for windows up to about
3.8 days, and 1 m works up to about 7.6 days.

## Layout: `<study>/telemetry/<id>/`

```
capture.json      what was captured, how, and sha256 of every file
summary.json      per-query reductions; measured claims cite this file
prom/<name>.json  the raw query_range response body, byte-for-byte
metrics.jsonl     the same data as OTel resourceMetrics, one line per series
```

`_telemetry.prom_path(capture_dir, name)` resolves `prom/<name>.json` even after
the size guard has moved it.

### `capture.json` (`capture_version: 1`)

```json
{
  "capture_version": 1,
  "source": {"kind": "url", "host": "thanos.example.com:9091",
             "auth": "bearer", "token_env": "PROM_TOKEN"},
  "cluster": "thanos.example.com:9091",
  "window": {"start": "2026-11-05T10:00:00Z", "end": "2026-11-05T11:00:00Z", "step": "30s"},
  "queries": {"vllm_running": "sum by (pod) (vllm:num_requests_running)"},
  "query_sources": [".../assets/promql/starter.yaml", "repo-config:promql_extra"],
  "empty_queries": ["vllm_ext_prefix_hit_ratio"],
  "failed_queries": {"bad_q": "bad_data: parse error ..."},
  "captured_at": "2026-11-05T11:02:13Z",
  "files": {"summary.json": "<sha256>", "prom/vllm_running.json": "<sha256>",
            "metrics.jsonl": "<sha256>"},
  "moved": {}
}
```

| Key | Meaning |
|---|---|
| `source.kind` | `url`, `port-forward`, or `import`. |
| `source.host` | `url`: the host and port only; never a path, query, token or credentials. `port-forward`: `<svc>.<ns>.svc:<port>`, plus `context`, `namespace`, `service` and `port`. The ephemeral local port is not recorded. |
| `source.auth`, `source.token_env` | Present only when `--token-env` was used. They record the env var's **name**, never its value. |
| `cluster` | The kube context for `port-forward`, or the host for `url`. When the host doesn't make the cluster obvious, name the cluster in the claim's `condition`. |
| `window` | The window actually queried. `step` uses the same format as `--step`. |
| `queries` | The exact merged `{name: promql}` that was run. |
| `query_sources` | Where the queries came from, in merge order. |
| `empty_queries` | Queries that succeeded with zero series. They are recorded, not failures. The raw file is still written. |
| `failed_queries` | `{name: message}` for queries where Prometheus returned `status: error` or the request failed. When Prometheus sent an error body, it is kept as `prom/<name>.json` (with the bearer token masked, if the body echoed it). Only a failure of every query exits nonzero. |
| `captured_at` | When the capture ran (wall clock, UTC). |
| `files` | `{relpath: sha256}` for every file still in the capture directory except `capture.json` itself. |
| `moved` | `{relpath: {sha256, path}}` for files the size guard moved to the runs dir. |

### `prom/<name>.json`

The body of `POST /api/v1/query_range` (`query`, `start`, `end`, `step`), saved
exactly as received: `{"status":"success","data":{"resultType":"matrix","result":[...]}}`.
This is the primary evidence. Claims can cite it directly, or cite `summary.json`.
A successful query's body is byte-for-byte; a failed query's error body is kept
with the bearer token masked (`***`) if the server echoed it back, so a
misbehaving gateway cannot smuggle the token into the study.

### `summary.json` (`summary_version: 1`)

`capture_prom.py` writes it at capture time, and `collect_telemetry.py` may
rewrite it in the same shape:

```json
{"summary_version": 1, "capture_id": "<id>", "window": {...},
 "queries": {"<name>": {"query": "<promql>",
                        "overall": {"mean": 0, "p50": 0, "max": 0, "min": 0, "n_points": 0, "n_series": 0},
                        "series": [{"mean": 0, "...": 0, "labels": {"pod": "..."}}]}}}
```

The reductions come from `_telemetry.summarize_query_range`. NaN and Inf samples
are dropped (`histogram_quantile` emits NaN on empty windows). An empty query has
`n_series: 0` and null stats. A failed query has the same null stats plus an `error` string.

### `metrics.jsonl`: the OTel mapping

There is one line per (query × series). Each line is an OTLP/JSON `MetricsData`
object holding a single `resourceMetrics` record:

```json
{"resourceMetrics": [{
  "resource": {"attributes": [
    {"key": "pod", "value": {"stringValue": "vllm-decode-0"}},
    {"key": "investigation.capture_id", "value": {"stringValue": "<id>"}},
    {"key": "investigation.query_name", "value": {"stringValue": "vllm_running"}}]},
  "scopeMetrics": [{"scope": {"name": "investigation.capture_prom", "version": "1"},
    "metrics": [{"name": "vllm_running", "description": "<promql>",
      "gauge": {"dataPoints": [{"timeUnixNano": "1762336800000000000", "asDouble": 3.0}]}}]}]}]}
```

- Every Prometheus series label becomes a resource attribute, as `stringValue`
  and sorted by key. Two `investigation.*` attributes are added.
- The metric name is the **query name**. The PromQL text goes in `description`.
- Every series is a `gauge`. Query results are already evaluated (rates,
  quantiles), so there is no counter or histogram to reconstruct.
- `timeUnixNano` is a decimal string, following OTLP/JSON's int64 rule, rounded
  to the millisecond. `asDouble` is the sample value. NaN and Inf samples are
  skipped, because JSON can't carry them, and so are samples whose *timestamp*
  can't be converted to nanoseconds at all (an int beyond float range, an
  over-range or NaN timestamp, or one whose millisecond multiply overflows) —
  the point has no representable slot. The raw `prom/` file still has them.
- An empty query writes no lines.

### Size guard

The guard is 50 MiB by default (`_telemetry.SIZE_GUARD_BYTES`). Set
`$INVESTIGATION_SIZE_GUARD_BYTES` to override it. When `prom/*.json`,
`metrics.jsonl` and `summary.json` together exceed the guard, `prom/` and
`metrics.jsonl` move to `<runs_dir>/telemetry-<id>/`:

- `<runs_dir>` is `_config.runs_dir(repo, slug)`, which is
  `$INVESTIGATION_RUNS/<repo>/<slug>/` (the repo is `_no-repo` outside git).
- The study keeps `capture.json` and `summary.json`. Each moved file's sha256 and
  absolute path are recorded under `moved`.

Measured claims cite `summary.json`, so they still validate. Anyone re-checking
can verify the moved raw files against `moved[*].sha256`.

## Starter queries

`assets/promql/starter.yaml` lists vLLM load, KV and prefix cache, latency
histograms, and throughput, plus the llm-d EPP routing and scheduling metrics.
Its header records how the names were checked: vLLM @ `2f1cba799e` and
llm-d-router @ `618eb0a9`. Two names differed from the design's list and were fixed:

- vLLM counters are scraped with a `_total` suffix.
- The disaggregation label is `decision_type`.

For now every study uses the same list. To add queries, use `--extra-queries`
or the repo's `promql_extra`.

## Claims from telemetry

A measured telemetry claim cites `telemetry/<id>/summary.json` or
`telemetry/<id>/prom/<name>.json`. Its `condition` must name the cluster, the
window and the query, e.g.
`"cluster kind-prod, 2026-11-05T10:00Z..11:00Z step 30s, vllm_ttft_p99"`.
`collect_telemetry.py` drafts these claims.

## Caveat: signal-pipeline and metrics-only directories (F1)

`signal-pipeline` itself is also a legacy gate, not runtime-enforced (see
`spotlights.md`).

Anomaly detection is spotlights' job, not this skill's. But `signal-pipeline
--telemetry-from` is built around trace-rich capture directories. **It does not
yet read a metrics-only directory** like the ones this script writes, which have
only `metrics.jsonl` and no traces or server logs. Until that gap is closed
upstream (owner to raise), do two things:

- In `plan.md`, mark `spotlights-telemetry` on a fresh `capture_prom.py`
  snapshot as unproven.
- Pair it with static Spotlights on the module that the metrics point at.

Even once it is supported, expect fewer and coarser candidates than trace-based
runs give: at the pod or metric level, not the function level.
