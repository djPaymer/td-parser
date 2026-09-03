from fastapi import APIRouter

from app.api.pipeline import router as pipeline_router
from app.core.config import settings

router = APIRouter(prefix=settings.api.v1.prefix)
router.include_router(pipeline_router)
