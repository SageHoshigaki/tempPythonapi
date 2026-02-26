from .events import router as events_router
from .recognize import router as recognize_router
from .upload import router as upload_router

__all__ = ["events_router", "recognize_router", "upload_router"]