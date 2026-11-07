
from contextlib import asynccontextmanager

from fastapi import FastAPI

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


app = FastAPI(
    lifespan=lifespan,
    title="Household Intelligence API",
    description="Household inventory and demand intelligence system",
    version="0.1.0",
)

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
