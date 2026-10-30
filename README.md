# Household Intelligence — Backend

## 1. Overview

The Household Intelligence backend is a REST API built with FastAPI. It provides authentication, database access, and household shopping-list management as part of an end-to-end household inventory and demand intelligence system.

**Current version:** `0.1.0`

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

## 2. Backend structure

```text
backend/
└── app/
    ├── main.py
    ├── api/
    │   └── routes/
    │       ├── auth.py
    │       └── shopping.py
                products.py
    ├── core/
    │   ├── config.py
    │   └── security.py
            units.py
    ├── db/
    │   ├── database.py
    │   └── models.py
    ├── schemas/
    │   ├── auth.py
    │   └── shopping.py
            products.py
    └── services/
        └── shopping_lists.py
            product_service.py
```

### Responsibilities

| Component | Responsibility |
|---|---|
| `main.py` | Creates the FastAPI application, registers routers, and exposes the health endpoint. |
| `api/routes/auth.py` | Registration, login, current-user retrieval, and authentication dependencies. |
| `api/routes/shopping.py` | Shopping-list and shopping-item API operations and access checks. |
| `core/config.py` | Loads and validates application settings and security configuration. |
| `core/security.py` | Password hashing and verification, and access-token creation and decoding. |
| `db/database.py` | SQLAlchemy engine, session factory, and database-session dependency. |
| `db/models.py` | SQLAlchemy database models and constraints. |
| `schemas/` | Request validation and response serialization. |
| `services/shopping_lists.py` | Shopping-list and shopping-item business operations. |
| `core/units.py` | The single list of supported units (`kg`, `g`, `liter`, `ml`, `piece`) used by validation and by the database CHECK constraints. |
| `services/product_service.py` | Product catalog operations (normalized-name uniqueness). |

## 3. Configuration

Create a `.env` file in the project root, alongside `pyproject.toml` and `docker-compose.yml`.

Required variables:

```dotenv
DATABASE_URL=your_postgresql_connection_string
JWT_SECRET_KEY=your_secure_random_secret_of_at_least_32_characters
```

Optional variables:

```dotenv
JWT_ALGORITHM=HS256
ACCESS_TOKEN_EXPIRE_MINUTES=15
```

Configuration rules:

- `DATABASE_URL` must point to the intended PostgreSQL database.
- `JWT_SECRET_KEY` must contain at least 32 characters and must not be a known placeholder.
- `JWT_ALGORITHM` currently supports `HS256`.
- Access-token expiration defaults to 15 minutes and must be greater than zero and no more than 60 minutes.
- The application loads `.env` from the project root.

**Security:** Never commit `.env` or expose database credentials or JWT secrets in source control.

## 4. Running locally

From the project root, ensure the required environment variables are configured.

Install or synchronize the project dependencies using uv:

```powershell
uv sync
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

The current Docker configuration builds the application using `pyproject.toml` and `uv.lock`, installs production dependencies, and starts Uvicorn.

The API is exposed on port `8000`.

## 6. Health check

### `GET /health`

Returns the API service health response.

Example:

```json
{
  "status": "healthy",
  "service": "household-intelligence-api"
}
```

A successful response confirms that the API process is responding. It does **not** independently verify database connectivity.

## 7. API endpoints

All authentication endpoints use the `/api/v1/auth` prefix. Shopping endpoints use `/api/v1`.

### Authentication

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/api/v1/auth/register` | Register a user. |
| `POST` | `/api/v1/auth/login` | Authenticate a user and return an access token. |
| `GET` | `/api/v1/auth/me` | Retrieve the authenticated user's profile. |

Registration returns HTTP `201` when successful. An already-registered email returns HTTP `409`.

Login returns a bearer access token. Invalid credentials return HTTP `401`.

Protected endpoints require:

```http
Authorization: Bearer <access_token>
```

### Shopping lists and items

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/api/v1/shopping-lists` | Create a personal shopping list. |
| `POST` | `/api/v1/households/{household_id}/shopping-list` | Create a household shopping list, subject to membership and role checks. |
| `GET` | `/api/v1/shopping-lists/{list_id}` | Retrieve an accessible active list. |
| `GET` | `/api/v1/shopping-lists/{list_id}/items` | Retrieve active items in an accessible list. |
| `POST` | `/api/v1/shopping-lists/{list_id}/items` | Add an item to an accessible list. |
| `PATCH` | `/api/v1/shopping-list-items/{item_id}/status` | Change an item's status. |
| `DELETE` | `/api/v1/shopping-list-items/{item_id}` | Archive an item. |
| `DELETE` | `/api/v1/shopping-lists/{list_id}` | Archive an accessible list, subject to the applicable permissions. |

The item-status endpoint accepts `needed` or `purchased`.

### Products

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/api/v1/products` | Create a catalog product. A duplicate normalized name returns `409`. |
| `GET` | `/api/v1/products/{product_id}` | Retrieve an active product. |

### Authorization behavior

- Users must authenticate before accessing protected shopping endpoints.
- Users can access their own personal lists and lists belonging to households they are members of.
- Household owners and administrators can create shared shopping lists.
- Only household owners and administrators can archive shared lists.
- Inaccessible and nonexistent lists or items return the same `404` response to reduce resource-existence disclosure.
- Database integrity conflicts are translated into appropriate conflict responses where handled by the API.

## 8. Database and sessions

SQLAlchemy manages database access through the configured PostgreSQL connection.

The database module provides:

- An engine configured with `pool_pre_ping=True`.
- A session factory named `SessionLocal`.
- A `get_db` dependency that closes each session after the request.

Database schema changes should be managed through the project's Alembic migrations rather than by manually modifying production tables.

### Item history (`item_events`)

`shopping_list_items.status` only stores the *current* state, so every change is also appended to `item_events`: `added`, `purchased`, `reverted` (purchased → needed) and `archived`. Each event copies the item's product, quantity and unit and records who did it and when (`occurred_at`).

- The table is **append-only**: rows are inserted, never updated or deleted.
- An event is written in the same transaction as the change it describes.
- Setting an item to the status it already has records nothing.
- Items that existed before this table received an `added` event at their real creation time. No `purchased` events were invented for them, because their real purchase time was never recorded.

This table is the source for the data engineering layer.

## 9. Testing

Run the project's default test suite from the root:

```powershell
uv run pytest
```

The pytest configuration excludes tests marked `integration` by default. These tests are kept separate because they depend on external services, such as Neon.

To run integration tests explicitly:

```powershell
uv run pytest -m integration
```

Run integration tests only when the required database and environment configuration are available.

## 10. Current scope

The current backend provides authentication, database models, and shopping-list and item operations.

The broader Household Intelligence roadmap includes data engineering, data-quality validation, analytics, demand modeling, and eventual product integration. These capabilities should be documented as completed only after their implementations and tests are in place.

## 11. Development principles

- Keep business logic in the service layer where appropriate.
- Validate request data through schemas.
- Enforce access control at the API boundary.
- Use database constraints to protect data integrity.
- Manage secrets through environment variables.
- Use migrations for schema evolution.
- Maintain automated tests as the system grows.
