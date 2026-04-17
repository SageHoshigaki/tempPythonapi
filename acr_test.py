import os
import time
import hmac
import base64
import hashlib
import tempfile
import wave
from typing import Optional, Dict, Any, Tuple

import httpx

# Load .env if available (pip install python-dotenv)
try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv()
except Exception:
    pass

# PyAV (pip install av)
import av  # type: ignore


HTTP_URI = "/v1/identify"
HTTP_METHOD = "POST"
DATA_TYPE = "audio"
SIGNATURE_VERSION = "1"

# ACRCloud guidance: < 15 seconds generally better; SDK uses ~10s often.
# We'll use 12s to be safely under 15 while giving a bit more context.
CLIP_SECONDS = 12.0
TARGET_SR = 44100
SAMPLE_WIDTH_BYTES = 2  # s16
CHANNELS = 1


def require_env(name: str) -> str:
    v = (os.getenv(name) or "").strip()
    if not v:
        raise RuntimeError(f"Missing env var: {name}")
    return v


def acr_signature(access_secret: str, access_key: str, timestamp: str) -> str:
    """
    ACRCloud signature = base64(hmac_sha1(secret, string_to_sign))
    string_to_sign = method + "\n" + uri + "\n" + access_key + "\n" + data_type + "\n" + version + "\n" + timestamp
    """
    string_to_sign = "\n".join([HTTP_METHOD, HTTP_URI, access_key, DATA_TYPE, SIGNATURE_VERSION, timestamp])
    digest = hmac.new(access_secret.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha1).digest()
    return base64.b64encode(digest).decode("utf-8")


def probe_duration_seconds(input_path: str) -> Optional[float]:
    try:
        c = av.open(input_path, options={"probesize": "20M", "analyzeduration": "20M"})
        dur = float(c.duration) / 1_000_000.0 if c.duration else None
        c.close()
        return dur
    except Exception:
        return None


def decode_clip_to_wav(
    input_path: str,
    clip_seconds: float,
    start_seconds: float,
    target_sr: int = TARGET_SR,
) -> str:
    """
    Decode a time window into mono s16 WAV @ 44.1kHz.
    Uses seek for speed and accuracy.
    """
    c = av.open(input_path, options={"probesize": "50M", "analyzeduration": "50M"})
    a = next((s for s in c.streams if s.type == "audio"), None)
    if not a:
        c.close()
        raise RuntimeError("No audio stream found in file")

    # Seek to start
    try:
        c.seek(int(start_seconds * 1_000_000), any_frame=True, backward=True, stream=a)
    except Exception:
        # If seek fails, decode from start (works but slower)
        pass

    resampler = av.audio.resampler.AudioResampler(format="s16", layout="mono", rate=target_sr)

    target_samples = int(target_sr * clip_seconds)
    got_samples = 0
    pcm = bytearray()

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
                    b = bytes(of.planes[0])
                    pcm.extend(b)
                    got_samples += (len(b) // SAMPLE_WIDTH_BYTES)
                    if got_samples >= target_samples:
                        break

                if got_samples >= target_samples:
                    break
            if got_samples >= target_samples:
                break
    finally:
        c.close()

    seconds_got = got_samples / target_sr
    if seconds_got < 6.0:
        raise RuntimeError(f"Clip too short after decode ({seconds_got:.2f}s).")

    fd, wav_path = tempfile.mkstemp(prefix="acr_clip_", suffix=".wav")
    os.close(fd)

    with wave.open(wav_path, "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(SAMPLE_WIDTH_BYTES)
        wf.setframerate(target_sr)
        wf.writeframes(bytes(pcm))

    return wav_path


def pick_middle_start(duration: Optional[float], clip_seconds: float) -> float:
    if not duration or duration <= clip_seconds:
        return 0.0
    return max(0.0, (duration / 2.0) - (clip_seconds / 2.0))


def summarize(out: Dict[str, Any]) -> str:
    status = out.get("status", {}) or {}
    code = status.get("code")
    msg = status.get("msg")

    music = (out.get("metadata", {}) or {}).get("music", []) or []
    if not music:
        return f"status={code} msg={msg} (no matches)"

    top = music[0]
    title = top.get("title")
    score = top.get("score")
    artists = ", ".join([a.get("name", "") for a in (top.get("artists", []) or []) if a.get("name")]) or None
    album = (top.get("album", {}) or {}).get("name")

    return f"status={code} msg={msg} | title={title} | artists={artists} | album={album} | score={score}"


async def identify_acrcloud(sample_wav_path: str) -> Dict[str, Any]:
    host = require_env("ACRCLOUD_HOST")  # e.g. identify-us-west-2.acrcloud.com
    access_key = require_env("ACRCLOUD_ACCESS_KEY")
    access_secret = require_env("ACRCLOUD_ACCESS_SECRET")

    url = f"https://{host}{HTTP_URI}"
    timestamp = str(int(time.time()))
    signature = acr_signature(access_secret=access_secret, access_key=access_key, timestamp=timestamp)

    sample_bytes = os.path.getsize(sample_wav_path)

    data = {
        "access_key": access_key,
        "data_type": DATA_TYPE,
        "signature_version": SIGNATURE_VERSION,
        "signature": signature,
        "timestamp": timestamp,
        "sample_bytes": str(sample_bytes),
    }

    async with httpx.AsyncClient(timeout=60) as client:
        with open(sample_wav_path, "rb") as f:
            files = {"sample": ("sample.wav", f, "audio/wav")}
            resp = await client.post(url, data=data, files=files)
            resp.raise_for_status()
            return resp.json()


async def main():
    input_file = "testrf.mp3"

    dur = probe_duration_seconds(input_file)
    start = pick_middle_start(dur, CLIP_SECONDS)

    print(f"[info] input={input_file}")
    print(f"[info] duration={dur if dur else 'unknown'}s")
    print(f"[info] using_clip_seconds={CLIP_SECONDS} (ACRCloud recommends <15s generally)")

    wav_path = None
    try:
        wav_path = decode_clip_to_wav(
            input_path=input_file,
            clip_seconds=CLIP_SECONDS,
            start_seconds=start,
            target_sr=TARGET_SR,
        )
        print(f"[info] clip_start={start:.2f}s -> wav={wav_path} bytes={os.path.getsize(wav_path)}")

        out = await identify_acrcloud(wav_path)

        print("\n=== SUMMARY ===")
        print(summarize(out))

        print("\n=== FULL JSON ===")
        print(out)

    finally:
        if wav_path and os.path.exists(wav_path):
            os.remove(wav_path)


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())