import math
import audioop
import wave
from typing import Tuple

from app.core.config import TARGET_SR, TARGET_CHANNELS, SAMPLE_WIDTH_BYTES, WINDOW_SECONDS

# Optional PyAV
try:
    import av  # type: ignore
    VALIDATE_WITH_PYAV = True
except ImportError:
    VALIDATE_WITH_PYAV = False


def ensure_pyav():
    if not VALIDATE_WITH_PYAV:
        raise RuntimeError("PyAV (av) not installed. Install: pip install av")


def probe_audio(path: str) -> dict:
    """Probe audio file properties so we can tell if it's silent/broken."""
    if not VALIDATE_WITH_PYAV:
        return {"pyav": False}

    try:
        c = av.open(path, options={"probesize": "50M", "analyzeduration": "50M"})
        a = next((s for s in c.streams if s.type == "audio"), None)

        info = {
            "pyav": True,
            "has_audio": a is not None,
            "codec": a.codec_context.name if a else None,
            "sample_rate": getattr(a.codec_context, "sample_rate", None) if a else None,
            "channels": getattr(a.codec_context, "channels", None) if a else None,
            "duration_seconds": (float(c.duration) / 1_000_000) if c.duration else None,
        }
        c.close()
        return info
    except Exception as e:
        return {"pyav": True, "error": str(e)}


def pcm_metrics_dbfs(pcm: bytes, sample_width: int = SAMPLE_WIDTH_BYTES) -> dict:
    """Compute RMS + peak in dBFS for PCM s16le mono."""
    if not pcm:
        return {"error": "empty_pcm"}

    rms = audioop.rms(pcm, sample_width)
    peak = audioop.max(pcm, sample_width)

    full_scale = float(2 ** (8 * sample_width - 1))  # 32768 for s16

    def to_dbfs(x: float) -> float:
        if x <= 1e-9:
            return -120.0
        return 20.0 * math.log10(x / full_scale)

    return {
        "rms_dbfs": round(to_dbfs(float(rms)), 2),
        "peak_dbfs": round(to_dbfs(float(peak)), 2),
        "rms": int(rms),
        "peak": int(peak),
        "bytes": len(pcm),
    }


def decode_to_pcm_s16_mono_44100(input_path: str, max_seconds: float = 30.0) -> Tuple[bytes, dict]:
    """
    Decode ANY audio file to PCM s16le mono 44.1k.
    Returns PCM bytes + debug info. No numpy required.
    """
    ensure_pyav()

    c = av.open(input_path, options={"probesize": "50M", "analyzeduration": "50M"})
    a = next((s for s in c.streams if s.type == "audio"), None)
    if not a:
        c.close()
        raise RuntimeError("No audio stream found in uploaded file")

    src_sr = a.codec_context.sample_rate or TARGET_SR
    resampler = av.audio.resampler.AudioResampler(format="s16", layout="mono", rate=TARGET_SR)

    target_samples = int(TARGET_SR * max_seconds)
    got_samples = 0
    out_pcm = bytearray()

    try:
        for packet in c.demux(a):
            for frame in packet.decode():
                frame.pts = None
                out_frames = resampler.resample(frame)
                if out_frames is None:
                    continue
                if not isinstance(out_frames, list):
                    out_frames = [out_frames]

                for of in out_frames:
                    plane_bytes = bytes(of.planes[0])  # FIX: bytes() not to_bytes()
                    out_pcm.extend(plane_bytes)
                    got_samples += (len(plane_bytes) // SAMPLE_WIDTH_BYTES)  # mono samples

                    if got_samples >= target_samples:
                        break
                if got_samples >= target_samples:
                    break
            if got_samples >= target_samples:
                break
    finally:
        c.close()

    info = {
        "src_sample_rate": src_sr,
        "target_sample_rate": TARGET_SR,
        "target_layout": "mono",
        "sample_width_bytes": SAMPLE_WIDTH_BYTES,
        "decoded_seconds_approx": round(got_samples / TARGET_SR, 3),
        "decoded_samples": got_samples,
        "decoded_bytes": len(out_pcm),
    }
    return bytes(out_pcm), info


def best_window_pcm(pcm: bytes, window_seconds: float = WINDOW_SECONDS, step_seconds: float = 0.5) -> Tuple[bytes, dict]:
    """Select the loudest continuous window (by RMS) from PCM s16 mono 44.1k."""
    if not pcm:
        return b"", {"error": "empty_pcm"}

    window_samples = int(TARGET_SR * window_seconds)
    window_bytes = window_samples * SAMPLE_WIDTH_BYTES

    if len(pcm) <= window_bytes:
        return pcm, {"selected": "full", "window_seconds": round(len(pcm) / (TARGET_SR * SAMPLE_WIDTH_BYTES), 3)}

    step_samples = max(1, int(TARGET_SR * step_seconds))
    step_bytes = step_samples * SAMPLE_WIDTH_BYTES

    best_rms = -1
    best_i = 0

    for i in range(0, len(pcm) - window_bytes + 1, step_bytes):
        chunk = pcm[i : i + window_bytes]
        rms = audioop.rms(chunk, SAMPLE_WIDTH_BYTES)
        if rms > best_rms:
            best_rms = rms
            best_i = i

    selected = pcm[best_i : best_i + window_bytes]
    meta = {
        "selected": "window",
        "window_seconds": window_seconds,
        "step_seconds": step_seconds,
        "start_seconds": round(best_i / (TARGET_SR * SAMPLE_WIDTH_BYTES), 3),
        "best_rms": int(best_rms),
    }
    return selected, meta


def write_wav(path: str, pcm: bytes) -> None:
    """Write PCM s16le mono 44.1k to WAV."""
    with wave.open(path, "wb") as wf:
        wf.setnchannels(TARGET_CHANNELS)
        wf.setsampwidth(SAMPLE_WIDTH_BYTES)
        wf.setframerate(TARGET_SR)
        wf.writeframes(pcm)