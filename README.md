# Chimera Backtest Tool

A minimal, stateless, AIM-compatible backtest service for Betfair historic markets.
The service consumes a strategy plugin (passed as JSON with each request — never baked
into code) plus a source pointer to historic data, and returns per-market results with
P&L. Intended to be invoked by an AI agent via HTTP — no portal logic lives here.

## Purpose

* Replay archived Betfair `.bz2` market change message (MCM) files.
* Apply a fully-configurable, JSON-driven strategy at a configured number of
  seconds before market off-time.
* Settle bets against the closing market state (using BSP when available).
* Persist results to GCS and expose them via a small REST API.

## Architecture

```
┌──────────────────────┐        ┌──────────────────────┐
│  AIM agent / portal  │ ─────► │   FastAPI service    │
└──────────────────────┘        │  (Cloud Run, Python  │
                                │       3.12)          │
                                └──────────────────────┘
                                  │     │             │
                ┌─────────────────┘     │             └──────────────┐
                ▼                       ▼                            ▼
   gs://betfair-basic-historic   gs://betfair-historic-adv    Betfair Historic API
   gs://chiops-backtest-results  ◄─── results.json + status.json
```

* `main.py` — FastAPI app exposing the three endpoint sets.
* `evaluator.py` — the pure strategy function.
* `services/` — GCS, Secret Manager, Betfair Historic, and the orchestrator.
* `core/` — config, structured logging, in-process job store, plugin store, event bus.
* `models/` — Pydantic schemas and immutable decision dataclasses.
* `plugins/` — strategy JSON files; the registry refreshes them at startup.

The service runs as a single Cloud Run instance. Long jobs run in-process via FastAPI
`BackgroundTasks` and a concurrency semaphore; all state lives in the in-memory
`JobStore` and is mirrored to GCS so a restart can rehydrate any job by id.

## API Surface

### Set 1 — Parameters (admin)

| Method | Path                            | Purpose                                          |
| ------ | ------------------------------- | ------------------------------------------------ |
| GET    | `/admin/status`                 | Health, version, uptime.                         |
| GET    | `/admin/config`                 | Effective default settings (renders settings UI).|
| PUT    | `/admin/config`                 | Update mutable defaults in-process.              |
| GET    | `/admin/stats`                  | Counters: jobs run, markets, bets, durations.    |
| GET    | `/admin/activity`               | Recent activity ring buffer (newest first).      |
| GET    | `/admin/control/{action}`       | Service controls (`cancel_job`, `clear_results_cache`). |
| GET    | `/admin/events`                 | Server-Sent Events stream.                       |

### Set 2 — GUI (portal-facing)

| Method | Path                                       | Purpose                                       |
| ------ | ------------------------------------------ | --------------------------------------------- |
| GET    | `/api/results`                             | Paginated job list, filterable.               |
| GET    | `/api/results/{result_id}`                 | Full result document.                         |
| GET    | `/api/results/{result_id}/markets`         | Per-market table for portal rendering.        |
| GET    | `/api/plugins`                             | Installed plugins.                            |
| GET    | `/api/plugins/{plugin_name}/schema`        | Editor schema for a specific plugin.          |

### Set 3 — Content (AIM agent)

| Method | Path                                                | Purpose                                  |
| ------ | --------------------------------------------------- | ---------------------------------------- |
| POST   | `/api/backtest`                                     | Submit a job; returns `job_id` immediately. |
| GET    | `/api/backtest/{job_id}`                            | Poll status (QUEUED, RUNNING, SUCCEEDED, FAILED, CANCELLED). |
| GET    | `/api/backtest/{job_id}/download/{json,xlsx,parquet}` | Download result in the requested format. |

### Request example — `POST /api/backtest`

The plugin block is the complete instruction set: it carries source, parser,
strategy, and staking. There is no separate top-level `source` field — the
plugin decides where data comes from. Every field is overridable at request
time; the saved plugin file simply provides the defaults the portal loads
when the user picks a plugin from the registry.

```json
{
  "plugin": {
    "name": "mark_4rule_lay_v1",
    "version": "1.0.0",
    "source": {
      "type": "gcs",
      "bucket": "gs://betfair-basic-historic/ADVANCED/",
      "date_range": { "start": "2025-01-01", "end": "2025-01-31" },
      "filters": {
        "countries": ["GB", "IE"],
        "market_types": ["WIN"]
      }
    },
    "parser": {
      "format": "betfair_mcm",
      "time_before_off_seconds": 300,
      "price_field": "ltp",
      "extract_bsp": true
    },
    "strategy": {
      "rules": [
        { "name": "rule_1", "odds_band": [1.50, 2.00], "base_stake": 3 },
        { "name": "rule_2", "odds_band": [2.00, 5.00], "base_stake": 2 },
        { "name": "rule_3a", "odds_band": [5.00, 8.00], "gap_lt": 2.0, "stake": 1, "also_lay_2nd": true },
        { "name": "rule_3b", "odds_band": [5.00, 8.00], "gap_gte": 2.0, "stake": 1 }
      ],
      "controls": {
        "hard_floor": 1.50,
        "hard_ceiling": 8.00,
        "jofs_enabled": true,
        "jofs_spread": 0.20,
        "mark_uplift": 2.0,
        "spread_control": true
      }
    },
    "staking": { "point_value": 7.50 }
  }
}
```

The 202 response carries the `job_id`. Poll `GET /api/backtest/{job_id}` until status is
`SUCCEEDED`, then call `GET /api/results/{job_id}` for the full document or
`GET /api/backtest/{job_id}/download/xlsx` for a workbook.

### Response example — `GET /api/results/{job_id}`

```json
{
  "job_id": "bt_20260101120000_a1b2c3",
  "status": "SUCCEEDED",
  "plugin": "mark_4rule_lay_v1",
  "plugin_version": "1.0.0",
  "duration_seconds": 342.0,
  "source_mode": "gcs",
  "summary": {
    "total_markets": 34,
    "total_bets": 28,
    "bets_won": 22,
    "bets_lost": 6,
    "strike_rate": 0.7857,
    "total_stake": 210.00,
    "total_liability": 294.00,
    "total_pnl": 45.30,
    "roi": 0.2157
  },
  "markets": [
    {
      "market_id": "1.234567890",
      "race_time": "2026-01-01T14:00:00Z",
      "venue": "Kempton",
      "country": "GB",
      "market_type": "WIN",
      "selection_id": 12345,
      "runner": "Horse Name",
      "bsp": 2.34,
      "lay_price": 2.40,
      "rule_applied": "rule_1",
      "side": "LAY",
      "stake": 22.50,
      "liability": 31.50,
      "outcome": "WON",
      "pnl": 22.50
    }
  ]
}
```

## Source modes

* **`gcs`** — read pre-downloaded `.bz2` files from a GCS bucket (default, fast,
  needs no Betfair login). Files are expected under
  `gs://<bucket>/<prefix>/YYYY/MM/DD/...`.
* **`betfair_historic`** — log into the Betfair Historic Data API with credentials
  pulled from Secret Manager, list and download files for the requested range, then
  stream them. Optionally mirror to GCS via `persist_to_bucket` so future runs can
  use `gcs` mode.

## Adding a new strategy plugin

A plugin is a single JSON file in `plugins/`. Drop a file conforming to the
`PluginConfig` schema (see `models/schemas.py`) and restart the service —
`PluginStore.refresh()` discovers it automatically.

```bash
cp plugins/mark_4rule_lay_v1.json plugins/my_new_strategy_v1.json
# edit name, version, rules, controls
gcloud run deploy backtest-tool ...
```

The new plugin appears at `GET /api/plugins` and its schema at
`GET /api/plugins/my_new_strategy_v1/schema`. Strategy logic stays in JSON;
no code changes required.

## Strategy contract

`evaluator.evaluate(market_book, strategy, *, point_value, filters_country,
filters_market_type)` is a **pure function**. It accepts a betfairlightweight
`MarketBook` plus the `strategy` block of a plugin, sorts the runners by
`last_price_traded`, applies the controls (hard floor / ceiling, spread control,
JOFS), then iterates the rule list looking for the first whose `odds_band`
contains the favourite price (and whose `gap_lt` / `gap_gte` constraints hold
against the spread to the 2nd favourite). The function returns a list of
`BetDecision` (one or two — JOFS and `also_lay_2nd` produce two) or a single
`NoBet` carrying a structured reason.

There are **zero hardcoded rules** and **zero rule-name string matches** in
the evaluator. Rule names are labels carried into the result for reporting
only — a plugin with rules named `banana_1`, `banana_2`, … runs through the
same code path as `rule_1`, `rule_2`, …. To change behaviour, change the JSON.

### Stake formula

```
final_stake = base_stake * mark_uplift * point_value
```

* `base_stake` (or `stake`) — read from the matched rule.
* `mark_uplift` — read from `strategy.controls`. Defaults to `1.0` when
  unset, so omitting it is a no-op.
* `point_value` — read from `staking`.

When JOFS splits the bet across the joint favourite and 2nd favourite, each
leg gets `final_stake / 2`. When `also_lay_2nd` is set on the rule, both the
favourite and 2nd favourite receive a full-stake lay.

### Custom controls

`StrategyControls` accepts unknown fields. A plugin that sets
`"magic_factor": 1.5` will validate and round-trip without code changes;
the evaluator simply ignores controls it doesn't know about. To make a new
control take effect, add a single read inside `evaluator.evaluate` —
nowhere else.

## Deployment

The repo deploys to Cloud Run via `gcloud run deploy`. Charles handles deploys
manually via the GCP Console after pushing to the GitHub repo:

```bash
gcloud run deploy backtest-tool \
  --source . \
  --region europe-west2 \
  --project chiops \
  --service-account backtest-tool@chiops.iam.gserviceaccount.com \
  --no-allow-unauthenticated
```

Secrets (`betfair-username`, `betfair-password`, `betfair-app-key`,
`betfair-cert-pem`, `betfair-key-pem`) are read at runtime from Secret Manager
via the bound service account; nothing is set as an environment variable.

### Required IAM bindings

Run once when the service account is created:

```bash
gcloud storage buckets add-iam-policy-binding gs://betfair-basic-historic \
  --member="serviceAccount:backtest-tool@chiops.iam.gserviceaccount.com" \
  --role="roles/storage.objectViewer"

gcloud storage buckets add-iam-policy-binding gs://betfair-historic-adv \
  --member="serviceAccount:backtest-tool@chiops.iam.gserviceaccount.com" \
  --role="roles/storage.objectViewer"

gcloud storage buckets add-iam-policy-binding gs://chiops-backtest-results \
  --member="serviceAccount:backtest-tool@chiops.iam.gserviceaccount.com" \
  --role="roles/storage.objectAdmin"

for SECRET in betfair-username betfair-password betfair-app-key betfair-cert-pem betfair-key-pem; do
  gcloud secrets add-iam-policy-binding "$SECRET" \
    --member="serviceAccount:backtest-tool@chiops.iam.gserviceaccount.com" \
    --role="roles/secretmanager.secretAccessor" \
    --project=chiops
done
```

### Environment

| Item                    | Value                                         |
| ----------------------- | --------------------------------------------- |
| GCP project             | `chiops`                                      |
| Region                  | `europe-west2`                                |
| Service account         | `backtest-tool@chiops.iam.gserviceaccount.com`|
| Results bucket          | `gs://chiops-backtest-results/`               |
| Source buckets          | `gs://betfair-basic-historic/`, `gs://betfair-historic-adv/` |
| Repo                    | `https://github.com/chimeracloud/backtest-tool.git` |
| Local path              | `/Users/charles/Projects/fsu-bt/`             |

The service reads optional configuration from environment variables prefixed
`CHIMERA_` (see `core/config.py`); deploy without overrides for production
defaults.

## Tests

```bash
pip install -r requirements.txt
pip install pytest
pytest tests/
```

The evaluator suite covers each rule in `mark_4rule_lay_v1`, the JOFS split, the
spread control gate, the floor/ceiling guards, and idempotency. The schema
suite exercises every validation rule documented above.

## Changelog

### 1.0.0 — initial release
* Three endpoint sets (`/admin`, GUI, content).
* GCS + Betfair Historic source modes.
* Pure-function strategy evaluator with rules driven entirely by JSON config.
* JSON / xlsx / parquet result downloads.
* Server-Sent Events stream for job lifecycle updates.
* Bundled `mark_4rule_lay_v1` plugin.
