from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import events_router, recognize_router, upload_router
from app.api.routes.dashboard import router as dashboard_router

app = FastAPI(title="MP4 Gateway (Modular)")
app.include_router(dashboard_router)
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

app.include_router(events_router)
app.include_router(recognize_router)
app.include_router(upload_router)