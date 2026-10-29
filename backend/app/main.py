
from fastapi import FastAPI
from backend.app.api.routes.shopping import router as shopping_router
from backend.app.api.routes.auth import router as auth_router
from backend.app.api.routes.products import router as products_router

app = FastAPI(
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

@app.get("/health")
def health_check():
    return {
        "status": "healthy",
        "service": "household-intelligence-api",
    }
