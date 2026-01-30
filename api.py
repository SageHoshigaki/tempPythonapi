import os
import tempfile
from uuid import uuid4

import httpx
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware

# Optional: validate MP4 container using PyAV (you said it's already in project)
# If you don't want validation, set VALIDATE_WITH_PYAV=0 in env or remove this block.
VALIDATE_WITH_PYAV = os.getenv("VALIDATE_WITH_PYAV", "0") == "1"
if VALIDATE_WITH_PYAV:
    import av  # PyAV


# ====== CONFIG (set these on Render as env vars) ======
# This is the "Step 1" endpoint from your API docs that returns a presigned URL.
# Example (from your screenshot): https://30d4lqyoxf.execute-api.us-east-1.amazonaws.com/prod/upload
INIT_UPLOAD_URL = os.getenv("INIT_UPLOAD_URL", "").strip()

# Optional: timeouts
HTTP_TIMEOUT_SECONDS = float(os.getenv("HTTP_TIMEOUT_SECONDS", "60"))


app = FastAPI(title="MP4 Gateway (UUID -> Presigned PUT)")

# Completely open CORS (any origin can call your API)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,  # keep False when allow_origins=["*"]
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/upload")
async def upload_mp4(file: UploadFile = File(...)):
    # ---- 1) Basic validation ----
    if not file.filename:
        raise HTTPException(status_code=400, detail="Missing filename")

    if not file.filename.lower().endswith(".mp4"):
        raise HTTPException(status_code=400, detail="Only .mp4 files are supported")

    if not INIT_UPLOAD_URL:
        raise HTTPException(
            status_code=500,
            detail="Server not configured: INIT_UPLOAD_URL env var is missing",
        )

    # ---- 2) Generate UUID ----
    file_id = uuid4().hex
    original_name = file.filename

    # ---- 3) Persist incoming upload to a temp file (safe + lets us know size) ----
    # Render allows writing to /tmp
    suffix = ".mp4"
    tmp_dir = tempfile.gettempdir()
    tmp_path = os.path.join(tmp_dir, f"{file_id}{suffix}")

    total_bytes = 0
    try:
        with open(tmp_path, "wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)  # 1MB chunks
                if not chunk:
                    break
                out.write(chunk)
                total_bytes += len(chunk)
    finally:
        await file.close()

    # ---- 4) Log to Render console: confirm it arrived + uuid ----
    print(f"[UPLOAD RECEIVED] uuid={file_id} filename={original_name} bytes={total_bytes}")

    # ---- 5) (Optional) Validate it's a readable MP4 container via PyAV ----
    if VALIDATE_WITH_PYAV:
        try:
            with av.open(tmp_path) as container:
                # Just opening it is enough to validate container structure
                stream_info = [(s.type, s.codec_context.name) for s in container.streams]
            print(f"[PYAV VALID] uuid={file_id} streams={stream_info}")
        except Exception as e:
            print(f"[PYAV INVALID] uuid={file_id} err={repr(e)}")
            raise HTTPException(status_code=400, detail="File is not a valid MP4 container")

    # ---- 6) Call Step 1: init upload (docs) ----
    # Docs want JSON like: { "filename": "name_of_file.mp4" }
    # We include the UUID in the filename so downstream is traceable.
    upstream_filename = f"{file_id}__{original_name}"

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
        init_resp = await client.post(INIT_UPLOAD_URL, json={"filename": upstream_filename})

        if init_resp.status_code >= 400:
            print(f"[INIT UPLOAD ERROR] status={init_resp.status_code} body={init_resp.text}")
            raise HTTPException(status_code=502, detail="Upstream init upload failed")

        init_data = init_resp.json()

        # Typical presign responses include one of these keys. Adjust if your docs differ.
        upload_url = (
            init_data.get("upload_url")
            or init_data.get("presigned_url")
            or init_data.get("url")
        )
        if not upload_url:
            print(f"[INIT UPLOAD BAD RESPONSE] body={init_data}")
            raise HTTPException(status_code=502, detail="Upstream did not return an upload URL")

        # Optional identifiers returned by your upstream
        upstream_file_key = init_data.get("file_key") or init_data.get("s3_key") or init_data.get("key")
        upstream_job_id = init_data.get("file_id") or init_data.get("job_id")

        print(f"[INIT UPLOAD OK] uuid={file_id} upstream_filename={upstream_filename}")

        # ---- 7) Step 2: PUT the MP4 bytes to presigned URL (raw binary) ----
        # IMPORTANT: this must be raw bytes, not multipart.
        headers = {
            "Content-Type": "video/mp4",
        }

        with open(tmp_path, "rb") as f:
            put_resp = await client.put(upload_url, content=f, headers=headers)

        if put_resp.status_code >= 400:
            print(f"[PRESIGNED PUT ERROR] status={put_resp.status_code} body={put_resp.text[:500]}")
            raise HTTPException(status_code=502, detail="Presigned PUT failed")

        print(f"[PRESIGNED PUT OK] uuid={file_id} bytes={total_bytes}")

    # ---- 8) Return to caller (your frontend) ----
    return {
        "uuid": file_id,
        "original_filename": original_name,
        "uploaded_as": upstream_filename,
        "bytes_received": total_bytes,
        "upstream_file_key": upstream_file_key,
        "upstream_job_id": upstream_job_id,
        "status": "received_and_uploaded",
    }


@app.get("/forward/{file_id}")
def forward_mp4(file_id: str):
    # skeleton for later
    print(f"[FORWARD] requested file_id={file_id}")
    return {"message": "forward skeleton", "file_id": file_id}