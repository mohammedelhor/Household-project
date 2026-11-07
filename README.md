# Household Intelligence

Household Intelligence is an end-to-end household inventory and demand-intelligence system, built in layers:

| Layer | Folder | Status |
|---|---|---|
| Backend (REST API, database, migrations) | `backend/` | Implemented |
| Data engineering (extraction, cleaning, quality gate, published tables) | `data_engineering/` | Implemented |
| Machine learning (baselines, evaluation, suggestions) | `ml/` | Implemented: rule-based baselines, no trained model yet |
| Frontend | — | Planned |
| AI agent | — | Planned |

**Current version:** `0.1.0`

## 1. Overview

The **backend** is a REST API built with FastAPI. It provides authentication, households, personal and household shopping lists, a product catalog, and an append-only history of every change to a list item (`item_events`).

The **data engineering** layer reads that history (read-only, through a dedicated database role), cleans it, validates it with a quality gate, and publishes versioned tables.

The **ML** layer turns the published purchases into, for each household (or user) and product, an expected date of the next purchase. It publishes a small JSON file; the backend serves it to the user without ever calling the ML or data layers.

```text
 PostgreSQL (app data)
        │  read-only role, 3 columns of shopping_lists + item_events + products
        ▼
 data_engineering.extract ──► data/raw/        (CSV, append-only copy + lists snapshots)
        ▼
 data_engineering.transform ─► quality gate ─► data/clean/runs/<run_id>/   (published tables)
        ▼
 ml.report   ──► data/experiments/*.json      (what was measured, on which data and code)
 ml.suggest --publish ──► data/serving/suggestions.json
        ▼
 backend  GET /api/v1/suggestions             (reads that one file; filters by the user's scopes)
```

### Technology stack

- Python 3.11
- FastAPI
- SQLAlchemy
- Alembic
- PostgreSQL (Neon)
- Pydantic Settings
- JWT-based authentication
- Argon2 password hashing
- uv for dependency management
- Docker and Docker Compose
- GitHub Actions (CI)

## 2. Project structure

```text
backend/
├── alembic.ini
├── migrations/versions/          # schema history (see section 9)
└── app/
    ├── main.py
    ├── api/routes/
    │   ├── auth.py               # register, login, current user
    │   ├── shopping.py           # personal and household lists, items
    │   ├── products.py           # product catalog
    │   └── suggestions.py        # GET /api/v1/suggestions
    ├── core/
    │   ├── config.py
    │   ├── security.py
    │   └── units.py              # the single list of supported units
    ├── db/
    │   ├── database.py
    │   ├── models.py             # users, households, household_members, products,
    │   │                         #   shopping_lists, shopping_list_items, item_events
    │   └── event_admin.py        # the explicit path for correcting item_events
    ├── schemas/
    │   ├── auth.py
    │   ├── shopping.py
    │   ├── products.py
    │   └── suggestions.py
    └── services/
        ├── shopping_lists.py     # list/item operations (also appends item_events)
        ├── product_service.py
        ├── suggestions.py        # reads the published suggestions file
        └── suggestion_lists.py   # the user's scopes and what is already on their lists

data_engineering/
├── extract.py                    # incremental, read-only extraction to CSV
├── transform.py                  # cleaning rules R1-R8, purchases, intervals, publishing
├── quality_gate.py               # critical/warning checks before anything is published
├── scopes.py                     # who owns a list: a household or a single user
├── contracts.py                  # table contracts (columns, types, rules)
├── lineage.py                    # input fingerprints, run ids, manifests
├── pipeline.py                   # extract + transform in one command
├── check_source_access.py        # proves the pipeline's database role is read-only
├── synthetic_data.py             # synthetic households with a known answer key
└── DATA_DICTIONARY.md            # generated from contracts.py

ml/
├── asof.py                       # rebuilds the past as it was known at a moment (no leakage)
├── target.py                     # the prediction task and samples
├── baselines.py                  # simple rules a model must beat
├── evaluate.py                   # metrics, evidence tiers, oracle reference
├── evidence.py                   # the single place for evidence thresholds
├── report.py                     # baseline report + experiment record
├── suggest.py                    # suggestions + the published JSON file
├── compare.py                    # paired comparison with a cluster bootstrap
├── power.py                      # how many samples are enough
└── PROTOCOL.md                   # task definition, comparison protocol, what each kind of data proves

tests/
├── unit/                         # no external services
└── integration/                  # need PostgreSQL (marked `integration`)
```

### Backend responsibilities

| Component | Responsibility |
|---|---|
| `main.py` | Creates the FastAPI application, registers routers, and exposes the health endpoint. |
| `api/routes/auth.py` | Registration, login, current-user retrieval, and authentication dependencies. |
| `api/routes/shopping.py` | Shopping-list and shopping-item API operations and access checks. |
| `api/routes/products.py` | Product catalog endpoints. |
| `api/routes/suggestions.py` | `GET /api/v1/suggestions`: the user's suggestions from the published file. |
| `core/config.py` | Loads and validates application settings and security configuration. |
| `core/security.py` | Password hashing and verification, and access-token creation and decoding. |
| `core/units.py` | The single list of supported units (`kg`, `g`, `liter`, `ml`, `piece`) used by validation and by the database CHECK constraints. |
| `db/database.py` | SQLAlchemy engine, session factory, and database-session dependency. |
| `db/models.py` | SQLAlchemy database models and constraints. |
| `db/event_admin.py` | `allow_event_correction(session)`: the explicit, transaction-local path for changing `item_events` (see section 8). |
| `schemas/` | Request validation and response serialization. |
| `services/shopping_lists.py` | Shopping-list and shopping-item business operations; appends `item_events`. |
| `services/product_service.py` | Product catalog operations (normalized-name uniqueness). |
| `services/suggestions.py` | Reads and validates the published suggestions file, drops other families' scopes, works out `status` and `days_until` for today. |
| `services/suggestion_lists.py` | The user's scopes (own + household) and the products waiting on their lists. |

## 3. Configuration

Create a `.env` file in the project root, alongside `pyproject.toml` and `docker-compose.yml`. `.env.example` shows the main ones.

Required for the API:

```dotenv
DATABASE_URL=your_postgresql_connection_string
JWT_SECRET_KEY=your_secure_random_secret_of_at_least_32_characters
```

Optional for the API:

```dotenv
JWT_ALGORITHM=HS256
ACCESS_TOKEN_EXPIRE_MINUTES=15
SUGGESTIONS_PATH=data/serving/suggestions.json
SUGGESTIONS_STALE_AFTER_DAYS=7
```

For the data and ML layers and the tests:

```dotenv
SOURCE_DATABASE_URL=...      # read-only role used by data_engineering.extract
TEST_DATABASE_URL=...        # a disposable PostgreSQL used by the integration tests
SYNTHETIC_DATABASE_URL=...   # a SEPARATE database for generated test data
DATA_ORIGIN=                 # real | synthetic, recorded in every manifest and experiment record
```

Configuration rules:

- `DATABASE_URL` must point to the intended PostgreSQL database.
- `JWT_SECRET_KEY` must contain at least 32 characters and must not be a known placeholder.
- `JWT_ALGORITHM` currently supports `HS256`.
- Access-token expiration defaults to 15 minutes and must be greater than zero and no more than 60 minutes.
- `SOURCE_DATABASE_URL` must be a least-privilege, read-only role. Check it with `uv run python -m data_engineering.check_source_access`.
- `SYNTHETIC_DATABASE_URL` must differ from `DATABASE_URL` and `TEST_DATABASE_URL`: the generator refuses to run otherwise.
- The application loads `.env` from the project root.

**Security:** Never commit `.env`, the `data/` folder, or expose database credentials or JWT secrets in source control.

## 4. Running locally

From the project root, ensure the required environment variables are configured.

Install or synchronize the project dependencies using uv:

```powershell
uv sync
```

Apply the database migrations:

```powershell
uv run alembic -c backend/alembic.ini upgrade head
```

Start the development server:

```powershell
uv run uvicorn backend.app.main:app --reload
```

The API should be available at:

- Base URL: `http://127.0.0.1:8000`
- Interactive Swagger documentation: `http://127.0.0.1:8000/docs`
- OpenAPI schema: `http://127.0.0.1:8000/openapi.json`

## 5. Running with Docker

Build the image:

```powershell
docker compose build
```

Start the service:

```powershell
docker compose up
```

Run it in the background:

```powershell
docker compose up -d
```

Stop the service:

```powershell
docker compose down
```

The current Docker configuration builds the application using `pyproject.toml` and `uv.lock`, installs production dependencies, and starts Uvicorn. It runs the API only; the data and ML layers are run from the command line (section 7).

The API is exposed on port `8000`.

## 6. API

### Health check: `GET /health`

Returns the API service health response.

```json
{
  "status": "healthy",
  "service": "household-intelligence-api"
}
```

A successful response confirms that the API process is responding. It does **not** independently verify database connectivity.

### Endpoints

Authentication endpoints use the `/api/v1/auth` prefix. All other endpoints use `/api/v1`.

#### Authentication

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/api/v1/auth/register` | Register a user. |
| `POST` | `/api/v1/auth/login` | Authenticate a user and return an access token. |
| `GET` | `/api/v1/auth/me` | Retrieve the authenticated user's profile. |

Registration returns HTTP `201` when successful. An already-registered email returns HTTP `409`. Login returns a bearer access token; invalid credentials return HTTP `401`. Protected endpoints require:

```http
Authorization: Bearer <access_token>
```

#### Shopping lists and items

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/api/v1/shopping-lists` | Create a personal shopping list. |
| `POST` | `/api/v1/households/{household_id}/shopping-list` | Create a household shopping list, subject to membership and role checks. |
| `GET` | `/api/v1/shopping-lists/{list_id}` | Retrieve an accessible active list. |
| `GET` | `/api/v1/shopping-lists/{list_id}/items` | Retrieve active items in an accessible list. |
| `POST` | `/api/v1/shopping-lists/{list_id}/items` | Add an item to an accessible list. |
| `PATCH` | `/api/v1/shopping-list-items/{item_id}/status` | Change an item's status (`needed` or `purchased`). |
| `DELETE` | `/api/v1/shopping-list-items/{item_id}` | Archive an item. |
| `DELETE` | `/api/v1/shopping-lists/{list_id}` | Archive an accessible list, subject to the applicable permissions. |

#### Products

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/api/v1/products` | Create a catalog product. A duplicate normalized name returns `409`. |
| `GET` | `/api/v1/products/{product_id}` | Retrieve an active product. |

#### Suggestions

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/v1/suggestions` | Which products the user's own scope and household will probably need soon. |

Each suggestion carries `scope_type` / `scope_id` (a `household` or a single `user`), the product, `expected_date`, `typical_gap_days`, `history_strength`, `gaps_used`, and three values worked out per request: `days_until`, `status` (`overdue`, `due_soon`, `not_yet`, `unknown`) and `already_on_list` / `on_list_id` (from the user's current lists). `history_strength` (`none`, `short`, `moderate`, `long`) says how much history was used; it is **not** a probability of being right. The response also reports `generated_at`, `dataset_run_id` and `stale` (the file is older than `SUGGESTIONS_STALE_AFTER_DAYS`). A missing or unreadable file returns `503` without exposing paths.

### Authorization behavior

- Users must authenticate before accessing protected endpoints.
- Users can access their own personal lists and lists belonging to households they are members of.
- Suggestions are filtered to the user's own scope and their household's. Another family's suggestions are never returned.
- Household owners and administrators can create shared shopping lists.
- Only household owners and administrators can archive shared lists.
- Inaccessible and nonexistent lists or items return the same `404` response to reduce resource-existence disclosure.
- Database integrity conflicts are translated into appropriate conflict responses where handled by the API.

## 7. Data pipeline and ML

Run these from the project root. Details and reasoning are in the module docstrings, `data_engineering/DATA_DICTIONARY.md` and `ml/PROTOCOL.md`.

### Concepts

- **Scope**: the owner of a shopping list, either a household or a single user (`household:3`, `user:7`). Predictions are made per scope and product; histories of different scopes are never combined.
- **Purchase**: an item that is still marked purchased at the end of its event history. A purchase mark is not a consumption measurement; every output built on it is a *suggestion to add the product to the list*, not a claim that it ran out.
- **Quality gate**: critical checks stop publication (the previous published run stays current); warnings are reported.
- **Run**: an immutable folder `data/clean/runs/<run_id>/`. The run id is derived from the input fingerprint and the code identity, so the same inputs and code give the same id. `data/clean/_latest.json` points to the newest run that passed the gate.

### Commands

```powershell
# 1. Check that the pipeline's database role is read-only (needs SOURCE_DATABASE_URL)
uv run python -m data_engineering.check_source_access

# 2. Extract and clean in one command
uv run python -m data_engineering.pipeline --data-origin real
#    (or separately: data_engineering.extract, then data_engineering.transform)

# 3. Baseline report: scores the rules on the latest published run, writes an experiment record
uv run python -m ml.report

# 4. Print today's suggestions, or publish the file the API serves
uv run python -m ml.suggest
uv run python -m ml.suggest --publish
```

`ml.report` and `ml.suggest` refuse to run if the raw files changed since the published run (run the pipeline first).

### Synthetic data (to verify the code)

```powershell
uv run python -m data_engineering.synthetic_data --households --cancel-rate 0.1 --write
uv run python -m data_engineering.pipeline --truth data_engineering/synthetic_ground_truth.json --data-origin synthetic
```

The generator writes to `SYNTHETIC_DATABASE_URL` and saves an *answer key*; `--truth` makes the pipeline compare its result with the key per scope. Synthetic data verifies the code, not performance on real households. Reports label it clearly.
After regenerating, the old synthetic rows are gone from the database but the pipeline's `data/raw` still holds its copies on purpose (extraction only ever adds). Move the old folder aside first (`Rename-Item data data_old_synthetic`), otherwise old and new households are counted together and every count is inflated.

### The contract between ML and the backend

`ml.suggest --publish` writes `data/serving/suggestions.json` (atomically). The backend reads this file and nothing else. Current `schema_version`: `2`.

```json
{
  "schema_version": 2,
  "generated_at": "2026-11-05T07:22:01+00:00",
  "as_of_date": "2026-11-05",
  "dataset": {"run_id": "3a23ed3dc2ae", "input_fingerprint": "…"},
  "suggestions": [
    {
      "scope_type": "household",
      "scope_id": 23,
      "product_id": 1,
      "product_name": "Eggs",
      "last_purchase_date": "2026-10-29",
      "typical_gap_days": 5,
      "expected_date": "2026-11-03",
      "history_strength": "long",
      "gaps_used": 37
    }
  ]
}
```

The file deliberately contains only what stays true: `status`, `days_until` and `already_on_list` change every day or with every list edit, so the API works them out per request. A different `schema_version` makes the API answer `503` rather than guess. Version 2 added the scope fields and renamed `confidence` to `history_strength`.

## 8. Database and sessions

SQLAlchemy manages database access through the configured PostgreSQL connection.

The database module provides:

- An engine configured with `pool_pre_ping=True`.
- A session factory named `SessionLocal`.
- A `get_db` dependency that closes each session after the request.

Database schema changes are managed through Alembic migrations rather than by manually modifying production tables. Existing migrations are never edited after they are released; a change is a new migration.

### Guarantees enforced by the database

| Guarantee | How |
|---|---|
| Only supported units are stored (`shopping_list_items`, `products`, `item_events`) | CHECK constraints generated from `core/units.py`; tests compare the database with the code. |
| A list has exactly one owner (a user **or** a household) | CHECK constraint `ck_shopping_list_exactly_one_owner`. |
| A list never changes owner | Trigger (migration `b7e2d41c9a05`). Predictions are per owner, so history must not move between families. |
| `item_events` is append-only | Trigger (migration `c4d8f2a61b73`), see below. |

### Item history (`item_events`)

`shopping_list_items.status` only stores the *current* state, so every change is also appended to `item_events`: `added`, `purchased`, `reverted` (purchased → needed) and `archived`. Each event copies the item's product, quantity and unit and records who did it and when (`occurred_at`).

- An event is written in the same transaction as the change it describes.
- Setting an item to the status it already has records nothing.
- Items that existed before this table received an `added` event at their real creation time. No `purchased` events were invented for them, because their real purchase time was never recorded.

**The table is append-only, and the database enforces it.** A trigger rejects `UPDATE`, `DELETE` and `TRUNCATE` on events. The single exception: `product_id` and `actor_user_id` are `ON DELETE SET NULL` references, so deleting a product or a user may set them to `NULL` (and nothing else on the row may change), which keeps the history when a user or product is removed.

To correct a wrong event, prefer **appending a new event** that fixes the state. If a row really must be changed or deleted (test cleanup, synthetic data, a reviewed manual correction), do it in one explicit transaction:

```sql
BEGIN;
SELECT set_config('household.allow_event_correction', 'on', true);  -- this transaction only
-- the UPDATE / DELETE
COMMIT;
```

In code: `allow_event_correction(session)` from `backend/app/db/event_admin.py`. Application code must never call it. The flag ends with the transaction.

*Limit:* this protects against mistakes, buggy code and ad-hoc scripts. A user who owns the table or is a superuser can still remove the trigger; the pipeline therefore connects with a read-only role.

This table is the source for the data engineering layer.

## 9. Migrations

```powershell
uv run alembic -c backend/alembic.ini upgrade head     # apply
uv run alembic -c backend/alembic.ini current          # what the database is on
uv run alembic -c backend/alembic.ini heads            # must list exactly one head
uv run alembic -c backend/alembic.ini check            # models vs migrations
```

`alembic check` does **not** compare CHECK constraints or triggers, so those are covered by tests (`tests/unit/test_model_constraints.py`, `tests/integration/test_unit_constraints_match_database.py`, `test_list_owner_trigger.py`, `test_item_events_append_only.py`).

The pipeline's read-only role needs access to three columns of `shopping_lists` (to know who owns each list):

```sql
GRANT SELECT (id, owner_user_id, household_id) ON shopping_lists TO <readonly_role>;
```

### Database roles

Three kinds of connection, each with the weakest role that can do its job:

| Role | Used by | Can do |
|---|---|---|
| owner (`neondb_owner`) | migrations, `scripts/setup_roles.py`, tests, the synthetic generator | everything; never used by the running API |
| `app_runtime` | the API (`DATABASE_URL`) | read/write normal tables; only `SELECT`/`INSERT` on `item_events`; no schema changes; cannot see `alembic_version` |
| `analytics_reader` | the data pipeline (`SOURCE_DATABASE_URL`) | `SELECT` on `item_events`, `products` and three owner columns of `shopping_lists` |

The trigger's correction flag (`household.allow_event_correction`) is **not** authorization: any role allowed to UPDATE/DELETE could switch it on. Privileges are the boundary, and `scripts/check_app_access.py` proves them (including that the flag does not help `app_runtime`).

```powershell
uv run python scripts/setup_roles.py dev            # look only
uv run python scripts/setup_roles.py dev --apply    # create roles + grants (idempotent)
uv run python scripts/check_app_access.py           # run as the API's role
```

Put the owner's URL in `OWNER_DATABASE_URL` (used by `scripts/migrate.py` and `setup_roles.py`) and the `app_runtime` URL in `DATABASE_URL`. Create roles with SQL (as the script does), not in the Neon console: console-made roles join `neon_superuser` and bypass table privileges. A new append-only table must be added to `APPEND_ONLY` in `scripts/setup_roles.py` and the script re-run.

### Environment labels

Our databases all look alike (same name, same user), so each one stores its own environment label (`dev`, `test`, `synthetic` or `production`) as a database comment, and dangerous tools ask the **database** who it is instead of trusting the URL: migrations, `setup_roles.py`, the test-suite, the synthetic generator and (when `EXPECTED_DB_ENVIRONMENT` is set) the API at startup. A wrong label stops the tool before anything changes. An unmarked database on this computer (CI, local docker) is accepted; an unmarked remote one is refused.

```powershell
uv run python scripts/mark_environment.py dev --apply   # once per database (production asks for the host)
uv run python scripts/inspect_databases.py              # shows each database's label
```


## 10. Testing

Run the default test suite (unit tests, no external services) from the root:

```powershell
uv run pytest
```

The pytest configuration excludes tests marked `integration` by default, because they need PostgreSQL. `TEST_DATABASE_URL` must point to a **disposable** database with the migrations applied.

```powershell
uv run pytest -m integration
```

CI (`.github/workflows/ci.yml`) starts a throwaway PostgreSQL 16, applies every migration from scratch, runs `alembic check`, then runs unit and integration tests together.

What the tests protect, beyond the usual behaviour checks:

- **No leakage:** the history shown to a predictor is rebuilt as known at the cutoff; future events (including those of other scopes) never change it.
- **No merging of families:** two households with very different rhythms for the same product are never combined, in any layer.
- **Synthetic answer key:** the pipeline reproduces the known answer per scope.
- **Database guarantees** listed in section 8.

## 11. Current scope

Implemented and tested: authentication, households and memberships, personal and household shopping lists, product catalog, the append-only item history, the data engineering pipeline with a quality gate and versioned outputs, ML baselines with an evaluation protocol, and the suggestions endpoint serving the published file.

Not implemented yet: a frontend, a scheduler for running the pipeline and publishing automatically, a trained prediction model (the current suggestions use a rule: last purchase date plus the median past interval), and the AI agent. Results on synthetic data verify the code only; no claim about performance on real households is made until real data with enough households exists (see `ml/PROTOCOL.md`).

## 12. Development principles

- Keep business logic in the service layer where appropriate.
- Validate request data through schemas.
- Enforce access control at the API boundary.
- Use database constraints and triggers to protect data integrity.
- In the data and ML layers only the extraction step reads the application database (read-only); everything after it reads files.
- Manage secrets through environment variables.
- Use migrations for schema evolution, and never edit a released one.
- Maintain automated tests as the system grows.
