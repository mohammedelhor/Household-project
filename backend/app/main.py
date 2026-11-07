from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.app.core.config import settings
from backend.app.core.db_safety import require_environment
from backend.app.api.routes.shopping import router as shopping_router
from backend.app.api.routes.auth import router as auth_router
from backend.app.api.routes.products import router as products_router
from backend.app.api.routes.suggestions import router as suggestions_router

@asynccontextmanager
async def lifespan(_: FastAPI):
    """Runs once at startup. With EXPECTED_DB_ENVIRONMENT set, the API refuses
    to start on a database that says it belongs to another environment."""
    if settings.expected_db_environment:
        require_environment(settings.database_url, settings.expected_db_environment)
    yield


def configure_cors(application: FastAPI, origins: list[str]) -> None:
    """Let the web front end (served from another origin) call this API.

    Browsers refuse a page from one origin reading answers from another unless
    the API says so. We say so ONLY for the origins listed in CORS_ORIGINS.
    With no origins listed we add nothing, so the API is same-origin only.

    allow_credentials=False on purpose: we authenticate with an
    `Authorization: Bearer` header, not cookies, so the browser never needs to
    attach credentials, and keeping this off removes a whole class of attacks.
    Methods and headers are the few we really use, not "*".
    """
    if not origins:
        return
    application.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["Authorization", "Content-Type"],
    )


app = FastAPI(
    lifespan=lifespan,
    title="Household Intelligence API",
    description="Household inventory and demand intelligence system",
    version="0.1.0",
)

configure_cors(app, settings.cors_origin_list)

app.include_router(
    auth_router,
    prefix="/api/v1/auth",
    tags=["Authentication"],
)
app.include_router(
    shopping_router,
    prefix="/api/v1",
    tags=["Shopping"],
)
app.include_router(
    products_router,
    prefix="/api/v1",
    tags=["Products"],
)

app.include_router(
    suggestions_router,
    prefix="/api/v1",
    tags=["Suggestions"],
)

@app.get("/health")
def health_check():
    return {
        "status": "healthy",
        "service": "household-intelligence-api",
    }