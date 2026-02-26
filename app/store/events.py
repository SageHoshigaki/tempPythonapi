import asyncio
import json
from typing import Dict, List

from fastapi import Request
from fastapi.responses import StreamingResponse

def sse(data: dict) -> str:
    return f"data: {json.dumps(data)}\n\n"

EVENT_LOGS: Dict[str, List[dict]] = {}
EVENT_QUEUES: Dict[str, asyncio.Queue] = {}

async def emit(client_id: str, event: dict):
    EVENT_LOGS.setdefault(client_id, []).append(event)
    q = EVENT_QUEUES.get(client_id)
    if q is not None:
        await q.put(event)

async def sse_event_stream(client_id: str, request: Request):
    q = EVENT_QUEUES.setdefault(client_id, asyncio.Queue())

    async def gen():
        yield sse({"step": "connected", "status": "ok", "client_id": client_id})

        for e in EVENT_LOGS.get(client_id, []):
            yield sse(e)

        while True:
            if await request.is_disconnected():
                break
            event = await q.get()
            yield sse(event)
            if event.get("step") in ("finished", "error"):
                break

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )