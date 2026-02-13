import os
import json
import tempfile
import logging
import asyncio
from uuid import uuid4
from typing import Dict, List, Optional

import httpx
from fastapi import FastAPI, UploadFile, File, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
from dotenv import load_dotenv

# ===== LOAD ENV =====
load_dotenv()
INIT_UPLOAD_URL = os.getenv("INIT_UPLOAD_URL", "").strip()
PROCESS_VIDEO_URL = os.getenv("PROCESS_VIDEO_URL", "").strip()
HTTP_TIMEOUT_SECONDS = float(os.getenv("HTTP_TIMEOUT_SECONDS", "60"))

# Optional MP4 validation
try:
    import av
    VALIDATE_WITH_PYAV = True
except ImportError:
    VALIDATE_WITH_PYAV = False

# ===== LOGGING =====
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
logger = logging.getLogger("mp4_gateway")

# ===== FASTAPI =====
app = FastAPI(title="MP4 Gateway (Upload + Separate SSE Events)")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

def sse(data: dict) -> str:
    return f"data: {json.dumps(data)}\n\n"

# In-memory per-job event store + live queues
EVENT_LOGS: Dict[str, List[dict]] = {}
EVENT_QUEUES: Dict[str, asyncio.Queue] = {}

async def emit(client_id: str, event: dict):
    """
    Record + publish events for a given client/job.
    NOTE: keep these small (don't include huge blobs).
    """
    EVENT_LOGS.setdefault(client_id, []).append(event)
    q = EVENT_QUEUES.get(client_id)
    if q is not None:
        await q.put(event)

@app.get("/")
def root():
    return {"status": "API running"}

@app.get("/health")
def health():
    return {"ok": True}

@app.get("/events/{client_id}")
async def events(client_id: str, request: Request):
    """
    Display component subscribes here. Streams events as they occur.
    Also replays any events already emitted (so if UI connects late, it still catches up).
    """
    q = EVENT_QUEUES.setdefault(client_id, asyncio.Queue())

    async def gen():
        # optional: immediate handshake
        yield sse({"step": "connected", "status": "ok", "client_id": client_id})

        # replay existing events first
        for e in EVENT_LOGS.get(client_id, []):
            yield sse(e)

        # then stream new ones
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

@app.post("/upload")
async def upload(file: UploadFile = File(...)):
    """
    UploadPortal calls this.
    Returns client_id immediately; processing continues in background.
    """
    if not file.filename:
        return JSONResponse({"error": "Missing filename"}, status_code=400)
    if not file.filename.lower().endswith(".mp4"):
        return JSONResponse({"error": "Only .mp4 allowed"}, status_code=400)
    if not INIT_UPLOAD_URL:
        return JSONResponse({"error": "Server misconfigured: INIT_UPLOAD_URL missing"}, status_code=500)
    if not PROCESS_VIDEO_URL:
        return JSONResponse({"error": "Server misconfigured: PROCESS_VIDEO_URL missing"}, status_code=500)

    client_id = uuid4().hex
    EVENT_LOGS[client_id] = []
    EVENT_QUEUES[client_id] = asyncio.Queue()

    # spool upload to disk (avoids holding big files in RAM during upload)
    job_file_id = uuid4().hex
    original_name = file.filename
    tmp_path = os.path.join(tempfile.gettempdir(), f"{job_file_id}.mp4")

    total_bytes = 0
    try:
        with open(tmp_path, "wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)  # 1MB
                if not chunk:
                    break
                out.write(chunk)
                total_bytes += len(chunk)
        await file.close()
        logger.info(f"[{client_id}] Saved upload: {original_name} ({total_bytes} bytes) -> {tmp_path}")
    except Exception as e:
        logger.exception("Failed to save upload")
        return JSONResponse({"error": str(e)}, status_code=500)

    # kick off background job
    asyncio.create_task(
        run_pipeline(
            client_id=client_id,
            job_file_id=job_file_id,
            original_name=original_name,
            tmp_path=tmp_path,
            total_bytes=total_bytes,
        )
    )

    return {"client_id": client_id}

async def run_pipeline(
    client_id: str,
    job_file_id: str,
    original_name: str,
    tmp_path: str,
    total_bytes: int
):
    streams: Optional[list] = None
    upstream_file_key: Optional[str] = None
    upstream_job_id: Optional[str] = None
    processed_key: Optional[str] = None

    try:
        await emit(client_id, {"step": 1, "status": "complete", "size_bytes": total_bytes})

        # Step 2: optional validation
        if VALIDATE_WITH_PYAV:
            await emit(client_id, {"step": 2, "status": "validating"})
            with av.open(tmp_path) as container:
                streams = [{"type": s.type, "codec": s.codec_context.name} for s in container.streams]
            await emit(client_id, {"step": 2, "status": "complete", "streams": streams})

        upstream_filename = f"{job_file_id}__{original_name}"

        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
            # Step 3: presigned URL
            await emit(client_id, {"step": 3, "status": "requesting_upload_url"})
            init_resp = await client.post(INIT_UPLOAD_URL, json={"filename": upstream_filename})
            init_resp.raise_for_status()
            init_data = init_resp.json()

            upload_url = init_data.get("upload_url") or init_data.get("presigned_url") or init_data.get("url")
            upstream_file_key = init_data.get("file_key") or init_data.get("s3_key") or init_data.get("key")
            upstream_job_id = init_data.get("file_id") or init_data.get("job_id")
            process_url = init_data.get("process_url") or PROCESS_VIDEO_URL

            if not upload_url:
                raise ValueError("Presigned upload URL missing")
            if not process_url:
                raise ValueError("Processing URL missing")

            await emit(client_id, {"step": 3, "status": "complete", "storage_key": upstream_file_key})

            # Step 4: upload to S3
            # IMPORTANT: AsyncClient cannot accept a sync file handle as `content=`.
            # We'll read the file bytes and send them.
            await emit(client_id, {"step": 4, "status": "uploading_to_storage"})

            with open(tmp_path, "rb") as f:
                data = f.read()

            put_resp = await client.put(
                upload_url,
                content=data,
                headers={"Content-Type": "video/mp4"}
            )
            put_resp.raise_for_status()

            await emit(client_id, {"step": 4, "status": "complete"})

            # Step 5: trigger processing
            await emit(client_id, {"step": 5, "status": "triggering_processing"})
            process_resp = await client.post(
                process_url,
                json={"s3_key": upstream_file_key, "file_id": upstream_job_id}
            )
            process_resp.raise_for_status()
            process_data = process_resp.json()
            processed_key = process_data.get("s3_key")

            await emit(client_id, {
                "step": 5,
                "status": "complete",
                "job_id": upstream_job_id,
                "processing_response": process_data
            })

            # finished
            await emit(client_id, {
                "step": "finished",
                "file_id": job_file_id,
                "original_filename": original_name,
                "size_bytes": total_bytes,
                "storage_key": upstream_file_key,
                "streams": streams,
                "job_id": upstream_job_id,
                "processed_key": processed_key,
            })

    except Exception as e:
        logger.exception("Pipeline error")
        await emit(client_id, {"step": "error", "message": str(e)})

    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
                logger.info(f"[{client_id}] Temp file cleaned up")
            except Exception:
                logger.warning(f"[{client_id}] Failed to clean temp file")