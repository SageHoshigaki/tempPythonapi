import os
import tempfile
from uuid import uuid4
import logging

import httpx
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware

# Optional MP4 validation
try:
    import av
    VALIDATE_WITH_PYAV = True
except ImportError:
    VALIDATE_WITH_PYAV = False

# ====== CONFIG ======
INIT_UPLOAD_URL = os.getenv("INIT_UPLOAD_URL", "").strip()
HTTP_TIMEOUT_SECONDS = float(os.getenv("HTTP_TIMEOUT_SECONDS", "60"))

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

app = FastAPI(title="MP4 Gateway (UUID -> Presigned PUT)")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
def root():
    return {"status": "API running"}


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/upload")
async def upload_mp4(file: UploadFile = File(...)):
    if not file.filename:
        raise HTTPException(status_code=400, detail="Missing filename")
    if not file.filename.lower().endswith(".mp4"):
        raise HTTPException(status_code=400, detail="Only .mp4 files are supported")
    if not INIT_UPLOAD_URL:
        raise HTTPException(status_code=500, detail="Server misconfigured: INIT_UPLOAD_URL missing")

    file_id = uuid4().hex
    original_name = file.filename
    tmp_path = os.path.join(tempfile.gettempdir(), f"{file_id}.mp4")
    total_bytes = 0

    try:
        # Save file to temp
        with open(tmp_path, "wb") as out_file:
            while chunk := await file.read(1024 * 1024):
                out_file.write(chunk)
                total_bytes += len(chunk)
        await file.close()

        logger.info(f"[UPLOAD RECEIVED] uuid={file_id} filename={original_name} bytes={total_bytes}")

        # Optional PyAV validation
        if VALIDATE_WITH_PYAV:
            try:
                with av.open(tmp_path) as container:
                    streams = [(s.type, s.codec_context.name) for s in container.streams]
                logger.info(f"[PYAV VALID] uuid={file_id} streams={streams}")
            except Exception as e:
                logger.error(f"[PYAV INVALID] uuid={file_id} error={repr(e)}")
                raise HTTPException(status_code=400, detail="Invalid MP4 file")

        # Init upload
        upstream_filename = f"{file_id}__{original_name}"
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
            try:
                init_resp = await client.post(INIT_UPLOAD_URL, json={"filename": upstream_filename})
                init_resp.raise_for_status()
                init_data = init_resp.json()
            except httpx.HTTPError as e:
                logger.error(f"[INIT UPLOAD ERROR] uuid={file_id} error={repr(e)}")
                raise HTTPException(status_code=502, detail="Failed to initialize upload")

            upload_url = init_data.get("upload_url") or init_data.get("presigned_url") or init_data.get("url")
            if not upload_url:
                logger.error(f"[INIT UPLOAD BAD RESPONSE] uuid={file_id} body={init_data}")
                raise HTTPException(status_code=502, detail="Upstream did not return an upload URL")

            upstream_file_key = init_data.get("file_key") or init_data.get("s3_key") or init_data.get("key")
            upstream_job_id = init_data.get("file_id") or init_data.get("job_id")
            logger.info(f"[INIT UPLOAD OK] uuid={file_id} upstream_filename={upstream_filename}")

            # Upload to presigned URL
            headers = {"Content-Type": "video/mp4"}
            try:
                with open(tmp_path, "rb") as f:
                    put_resp = await client.put(upload_url, content=f, headers=headers)
                    put_resp.raise_for_status()
                logger.info(f"[PRESIGNED PUT OK] uuid={file_id} bytes={total_bytes}")
            except httpx.HTTPError as e:
                logger.error(f"[PRESIGNED PUT ERROR] uuid={file_id} error={repr(e)}")
                raise HTTPException(status_code=502, detail="Failed to upload to presigned URL")

        return {
            "uuid": file_id,
            "original_filename": original_name,
            "uploaded_as": upstream_filename,
            "bytes_received": total_bytes,
            "upstream_file_key": upstream_file_key,
            "upstream_job_id": upstream_job_id,
            "status": "received_and_uploaded",
        }

    finally:
        # Ensure temp file is cleaned up
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
                logger.debug(f"[TEMP FILE REMOVED] {tmp_path}")
            except Exception as e:
                logger.warning(f"[TEMP FILE DELETE FAILED] path={tmp_path} error={repr(e)}")