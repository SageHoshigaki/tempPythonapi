import os
import tempfile
import asyncio
from uuid import uuid4
from typing import Optional

from fastapi import APIRouter, UploadFile, File, Form
from fastapi.responses import JSONResponse

from app.core.config import KEEP_DEBUG_FILES, TARGET_SR, SAMPLE_WIDTH_BYTES
from app.core.logging import setup_logging
from app.store.events import EVENT_LOGS, EVENT_QUEUES, emit
from app.services.audio_processing import (
    ensure_pyav,
    probe_audio,
    decode_to_pcm_s16_mono_44100,
    best_window_pcm,
    pcm_metrics_dbfs,
    write_wav,
)
from app.services.audd import recognize_with_audd

logger = setup_logging()
router = APIRouter(tags=["recognize"])


@router.post("/recognize")
async def recognize(
    file: UploadFile = File(...),
    mode: str = Form("mic"),  # "mic" or "tab"
):
    if not file.filename:
        return JSONResponse({"error": "Missing filename"}, status_code=400)

    client_id = uuid4().hex
    EVENT_LOGS[client_id] = []
    EVENT_QUEUES[client_id] = asyncio.Queue()

    job_id = uuid4().hex
    original_name = file.filename
    tmp_path = os.path.join(tempfile.gettempdir(), f"{job_id}__{original_name}")

    total_bytes = 0
    content_type = (file.content_type or "").strip() or "application/octet-stream"

    try:
        with open(tmp_path, "wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)
                total_bytes += len(chunk)
        await file.close()
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

    asyncio.create_task(
        run_recognize_pipeline(
            client_id=client_id,
            job_id=job_id,
            original_name=original_name,
            tmp_path=tmp_path,
            total_bytes=total_bytes,
            content_type=content_type,
            mode=(mode or "mic").strip().lower(),
        )
    )

    return {"client_id": client_id}


async def run_recognize_pipeline(
    client_id: str,
    job_id: str,
    original_name: str,
    tmp_path: str,
    total_bytes: int,
    content_type: str,
    mode: str,
):
    wav_path: Optional[str] = None
    pcm_all: Optional[bytes] = None

    try:
        await emit(
            client_id,
            {
                "step": 1,
                "status": "received",
                "filename": original_name,
                "size_bytes": total_bytes,
                "content_type": content_type,
                "mode": mode,
            },
        )

        ensure_pyav()

        await emit(client_id, {"step": 2, "status": "probing_input"})
        probe_in = probe_audio(tmp_path)
        logger.info(f"[INPUT PROBE] {probe_in}")
        await emit(client_id, {"step": 2.1, "status": "input_probe", **probe_in})

        max_decode_seconds = 30.0 if mode == "mic" else 18.0

        await emit(client_id, {"step": 3, "status": "decoding_to_pcm", "max_seconds": max_decode_seconds})
        pcm_all, decode_info = await asyncio.to_thread(decode_to_pcm_s16_mono_44100, tmp_path, max_decode_seconds)
        await emit(client_id, {"step": 3.1, "status": "decoded", **decode_info})

        if not pcm_all or len(pcm_all) < (TARGET_SR * SAMPLE_WIDTH_BYTES * 3):
            raise RuntimeError("Decoded audio too short/small — likely silence, permission issue, or bad capture.")

        await emit(client_id, {"step": 3.2, "status": "selecting_best_window"})
        pcm_best, win_meta = await asyncio.to_thread(best_window_pcm, pcm_all)
        await emit(client_id, {"step": 3.25, "status": "window_selected", **win_meta})

        full_metrics = pcm_metrics_dbfs(pcm_all)
        best_metrics = pcm_metrics_dbfs(pcm_best)
        await emit(client_id, {"step": 3.3, "status": "metrics_full", **full_metrics})
        await emit(client_id, {"step": 3.31, "status": "metrics_selected", **best_metrics})

        if isinstance(best_metrics.get("rms_dbfs"), (int, float)) and best_metrics["rms_dbfs"] < -40.0:
            raise RuntimeError(
                f"Audio too quiet for recognition (RMS {best_metrics['rms_dbfs']} dBFS). "
                "Move speaker closer to mic, raise volume (avoid distortion), and record again."
            )

        wav_path = os.path.join(tempfile.gettempdir(), f"{job_id}__selected.wav")
        await emit(client_id, {"step": 3.4, "status": "writing_wav"})
        await asyncio.to_thread(write_wav, wav_path, pcm_best)

        send_bytes = os.path.getsize(wav_path)
        logger.info(f"[SEND DEBUG] name=recording.wav type=audio/wav bytes={send_bytes} path={wav_path}")
        await emit(
            client_id,
            {
                "step": 3.5,
                "status": "sending_to_audd",
                "send_name": "recording.wav",
                "send_type": "audio/wav",
                "send_file": os.path.basename(wav_path),
                "send_bytes": send_bytes,
            },
        )

        await emit(client_id, {"step": 4, "status": "calling_audd"})
        audd_json = await recognize_with_audd(wav_path)

        await emit(
            client_id,
            {
                "step": 4.5,
                "status": "audd_debug",
                "audd_status": audd_json.get("status"),
                "has_result": audd_json.get("result") is not None,
                "error": audd_json.get("error"),
            },
        )
        logger.info(
            f"[AUDD DEBUG] status={audd_json.get('status')} "
            f"has_result={audd_json.get('result') is not None} "
            f"error={audd_json.get('error')}"
        )

        result = audd_json.get("result") or {}
        song_title = result.get("title")
        artist = result.get("artist")
        album = result.get("album")
        score = result.get("score")
        matched = bool(song_title and artist)

        await emit(
            client_id,
            {
                "step": 5,
                "status": "complete",
                "matched": matched,
                "song_title": song_title,
                "artist": artist,
                "album": album,
                "score": score,
            },
        )
        await emit(
            client_id,
            {
                "step": "finished",
                "job_id": job_id,
                "original_filename": original_name,
                "bytes_received": total_bytes,
                "matched": matched,
                "song_title": song_title,
                "artist": artist,
                "album": album,
                "score": score,
                "audd_raw": audd_json,
            },
        )

    except Exception as e:
        logger.exception("recognize pipeline failed")
        await emit(client_id, {"step": "error", "message": str(e)})

    finally:
        if KEEP_DEBUG_FILES:
            return

        for p in [tmp_path, wav_path]:
            if p and os.path.exists(p):
                try:
                    os.remove(p)
                except Exception:
                    pass