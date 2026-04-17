import asyncio
from typing import Any, Dict, Optional

from app.services.audd import recognize_with_audd
from acrcloud import recognize_with_acrcloud

async def recognize_both(wav_path: str) -> Dict[str, Any]:
    """
    Returns:
      {
        "audd": {...},
        "acrcloud": {...}
      }
    """
    audd_task = asyncio.create_task(recognize_with_audd(wav_path))
    acr_task = asyncio.to_thread(recognize_with_acrcloud, wav_path)  # sync SDK -> thread

    audd_res, acr_res = await asyncio.gather(audd_task, acr_task, return_exceptions=True)

    def normalize(x):
        if isinstance(x, Exception):
            return {"error": str(x)}
        return x

    return {"audd": normalize(audd_res), "acrcloud": normalize(acr_res)}