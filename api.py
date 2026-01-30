from uuid import uuid4

from fastapi import FastAPI, UploadFile, File, HTTPException

app = FastAPI(title="MP4 Gateway (UUID only)")

@app.get("/health")
def health():
    return {"ok": True}

@app.post("/upload")
async def upload_mp4(file: UploadFile = File(...)):
    # minimal validation
    if not file.filename or not file.filename.lower().endswith(".mp4"):
        raise HTTPException(status_code=400, detail="Only .mp4 files are supported")

    file_id = uuid4().hex  # UUID attached to this upload

    # NOTE: we are NOT storing anything yet — just generating the id
    return {
        "file_id": file_id,
        "original_filename": file.filename,
        "content_type": file.content_type,
        "status": "received",
    }

@app.get("/forward/{file_id}")
def forward_mp4(file_id: str):
    # skeleton for later
    return {"message": "forward skeleton", "file_id": file_id}