from fastapi import APIRouter, Request
from app.store.events import sse_event_stream

router = APIRouter(tags=["events"])

@router.get("/events/{client_id}")
async def events(client_id: str, request: Request):
    return await sse_event_stream(client_id, request)