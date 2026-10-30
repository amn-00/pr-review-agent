from fastapi import APIRouter

router = APIRouter()


@router.get("/health")
async def health() -> dict[str, str]:
    """Liveness check: the process is up and serving requests. Checks no dependencies."""
    return {"status": "ok"}
