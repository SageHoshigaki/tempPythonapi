import httpx
from typing import Any, Dict

from app.core.config import AUDD_API_URL, AUDD_API_TOKEN


async def recognize_with_audd(wav_path: str) -> Dict[str, Any]:
    if not AUDD_API_TOKEN:
        raise RuntimeError("AUDD_API_TOKEN not set")

    async with httpx.AsyncClient(timeout=60) as client:
        with open(wav_path, "rb") as f:
            files = {"file": ("recording.wav", f, "audio/wav")}
            data = {"api_token": AUDD_API_TOKEN, "return": "spotify,apple_music"}
            resp = await client.post(AUDD_API_URL, data=data, files=files)
            resp.raise_for_status()
            return resp.json()