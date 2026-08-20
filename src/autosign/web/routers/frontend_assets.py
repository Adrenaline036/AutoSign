from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse

STATIC_DIR = Path(__file__).parents[1] / "static"


def create_frontend_assets_router() -> APIRouter:
    router = APIRouter()

    @router.get("/assets/accounts.js", include_in_schema=False)
    async def accounts_script() -> FileResponse:
        return FileResponse(
            STATIC_DIR / "accounts.js",
            media_type="text/javascript",
            headers={"Cache-Control": "no-store"},
        )

    return router