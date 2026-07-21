# Example Outputs

Captured from a real local run against `../../monthly_account_status.parquet`
(month `2026-01-01`, `ARR_THRESHOLD=10000`), with the app pointed at a locally
running `mock_slack` server (`MOCK_SLACK_FAIL_RATE_429=0.2`,
`MOCK_SLACK_FAIL_RATE_500=0.1`) to exercise retry/backoff for real.

- **`preview_response.json`** — `POST /preview` for `2026-01-01`. 141 At Risk,
  ARR-eligible accounts found; 137 would route to a channel, 4 have no
  `account_region` and would fail with `unknown_region`. No Slack calls are
  made. `duplicates_found: 82` reflects real `(account_id, month)` duplicate
  rows encountered while scanning the target month and candidate history.
- **`runs_post_response.json`** — `POST /runs` response shape: just `{"run_id": ...}`
  per spec; full results are fetched via `GET /runs/{run_id}`.
- **`runs_get_response.json`** — `GET /runs/{run_id}` for the first real send
  of `2026-01-01`. 136 sent, 4 failed with `unknown_region`, 1 failed after
  exhausting retries against transient mock 429/500s (see `error` field on
  the sample errors, e.g. `"HTTP 429: mock slack: rate limited"`). Exact
  counts vary run to run since `mock_slack` injects failures randomly.
- **`runs_get_response_replay.json`** — `GET /runs/{run_id}` for re-running
  the *same* `source_uri`/`month` immediately after. Replay safety: 136
  `skipped_replay` (already `sent`, not re-delivered). Retry-on-failed: the 1
  account that failed with a transient Slack error last time was retried
  and is now `sent` (`alerts_sent: 1`). The 4 `unknown_region` failures are
  retried and fail again every run, since routing config hasn't changed.
- **`support_notifications_sample.jsonl`** — the aggregated unknown-region
  notification (stub/logging mechanism, see main README) appended once per
  run that had `unknown_region` failures — one line per run above.
- **`mock_slack_requests_sample.jsonl`** — first few lines of `mock_slack`'s
  own request log for this run, showing real 200/429/500 responses.
