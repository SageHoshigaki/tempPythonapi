from uuid import uuid4

from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="MP4 Gateway (UUID only)")

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
    # minimal validation
    if not file.filename or not file.filename.lower().endswith(".mp4"):
        raise HTTPException(status_code=400, detail="Only .mp4 files are supported")

    file_id = uuid4().hex

    # Render logs: prints to your service logs
    print(f"[UPLOAD] uuid={file_id} filename={file.filename}")

    # NOTE: not storing the file yet—just generating an id
    return {
        "file_id": file_id,
        "original_filename": file.filename,
        "content_type": file.content_type,
        "status": "received",
    }

@app.get("/forward/{file_id}")
def forward_mp4(file_id: str):
    # skeleton for later
    print(f"[FORWARD] requested file_id={file_id}")
    return {"message": "forward skeleton", "file_id": file_id}