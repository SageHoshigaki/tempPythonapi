import os
import tempfile
import asyncio
from uuid import uuid4
from typing import Optional

import httpx
from fastapi import APIRouter, UploadFile, File
from fastapi.responses import JSONResponse

from app.core.config import INIT_UPLOAD_URL, PROCESS_VIDEO_URL, HTTP_TIMEOUT_SECONDS
from app.core.logging import setup_logging
from app.store.events import EVENT_LOGS, EVENT_QUEUES, emit

# Optional PyAV
try:
    import av  # type: ignore
    VALIDATE_WITH_PYAV = True
except ImportError:
    VALIDATE_WITH_PYAV = False

logger = setup_logging()
router = APIRouter(tags=["upload"])


@router.post("/upload")
async def upload(file: UploadFile = File(...)):
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

    job_file_id = uuid4().hex
    original_name = file.filename
    tmp_path = os.path.join(tempfile.gettempdir(), f"{job_file_id}.mp4")

    total_bytes = 0
    try:
        with open(tmp_path, "wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)
                total_bytes += len(chunk)
        await file.close()
        logger.info(f"[{client_id}] Saved upload: {original_name} ({total_bytes} bytes) -> {tmp_path}")
    except Exception as e:
        logger.exception("Failed to save upload")
        return JSONResponse({"error": str(e)}, status_code=500)

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
    total_bytes: int,
):
    streams: Optional[list] = None
    upstream_file_key: Optional[str] = None
    upstream_job_id: Optional[str] = None
    processed_key: Optional[str] = None

    try:
        await emit(client_id, {"step": 1, "status": "complete", "size_bytes": total_bytes})

        if VALIDATE_WITH_PYAV:
            await emit(client_id, {"step": 2, "status": "validating"})
            with av.open(tmp_path) as container:
                streams = [{"type": s.type, "codec": s.codec_context.name} for s in container.streams]
            await emit(client_id, {"step": 2, "status": "complete", "streams": streams})

        upstream_filename = f"{job_file_id}__{original_name}"

        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
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
            await emit(client_id, {"step": 4, "status": "uploading_to_storage"})

            with open(tmp_path, "rb") as f:
                data = f.read()

            put_resp = await client.put(upload_url, content=data, headers={"Content-Type": "video/mp4"})
            put_resp.raise_for_status()

            await emit(client_id, {"step": 4, "status": "complete"})
            await emit(client_id, {"step": 5, "status": "triggering_processing"})

            process_resp = await client.post(process_url, json={"s3_key": upstream_file_key, "file_id": upstream_job_id})
            process_resp.raise_for_status()
            process_data = process_resp.json()
            processed_key = process_data.get("s3_key")

            await emit(
                client_id,
                {"step": 5, "status": "complete", "job_id": upstream_job_id, "processing_response": process_data},
            )

            await emit(
                client_id,
                {
                    "step": "finished",
                    "file_id": job_file_id,
                    "original_filename": original_name,
                    "size_bytes": total_bytes,
                    "storage_key": upstream_file_key,
                    "streams": streams,
                    "job_id": upstream_job_id,
                    "processed_key": processed_key,
                },
            )

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