import os
import json
import time
import base64
import hmac
import hashlib
import requests
from dotenv import load_dotenv
from pydub import AudioSegment

# ----------------------------
# Load environment variables
# ----------------------------
load_dotenv()

ACRCLOUD_HOST = os.getenv("ACRCLOUD_HOST")
ACRCLOUD_ACCESS_KEY = os.getenv("ACRCLOUD_ACCESS_KEY")
ACRCLOUD_ACCESS_SECRET = os.getenv("ACRCLOUD_ACCESS_SECRET")

if not ACRCLOUD_HOST or not ACRCLOUD_ACCESS_KEY or not ACRCLOUD_ACCESS_SECRET:
    raise ValueError("Missing ACRCloud environment variables in .env")

# ----------------------------
# Config
# ----------------------------
SOURCE_FILE = "testrf.mp3"
CLIP_FILE = "testrf_clip.mp3"

# Chorus start: 1:26
START_SECONDS = 86
DURATION_SECONDS = 11


def create_clip(source_path: str, output_path: str, start_sec: int, duration_sec: int):
    """Create an MP3 clip from the source file."""
    if not os.path.exists(source_path):
        raise FileNotFoundError(f"Source file not found: {source_path}")

    audio = AudioSegment.from_file(source_path)
    start_ms = start_sec * 1000
    end_ms = (start_sec + duration_sec) * 1000

    if start_ms >= len(audio):
        raise ValueError("Start time is beyond the length of the audio file.")

    clip = audio[start_ms:end_ms]
    clip.export(output_path, format="mp3", bitrate="320k")

    print(f"Created clip: {output_path}")
    print(f"Clip start: {start_sec}s")
    print(f"Clip duration: {duration_sec}s")
    print(f"Clip size: {os.path.getsize(output_path)} bytes\n")


def build_signature(http_method: str, http_uri: str, access_key: str, data_type: str, signature_version: str, timestamp: str, access_secret: str) -> str:
    """
    ACRCloud signature:
    string_to_sign = HTTP_METHOD + "\\n" + HTTP_URI + "\\n" + ACCESS_KEY + "\\n" + DATA_TYPE + "\\n" + SIGNATURE_VERSION + "\\n" + TIMESTAMP
    """
    string_to_sign = "\n".join([
        http_method,
        http_uri,
        access_key,
        data_type,
        signature_version,
        timestamp,
    ])

    sign = hmac.new(
        access_secret.encode("utf-8"),
        string_to_sign.encode("utf-8"),
        digestmod=hashlib.sha1
    ).digest()

    return base64.b64encode(sign).decode("utf-8")


def recognize_song_with_acrcloud(file_path: str):
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"Clip file not found: {file_path}")

    http_method = "POST"
    http_uri = "/v1/identify"
    data_type = "audio"
    signature_version = "1"
    timestamp = str(int(time.time()))

    signature = build_signature(
        http_method=http_method,
        http_uri=http_uri,
        access_key=ACRCLOUD_ACCESS_KEY,
        data_type=data_type,
        signature_version=signature_version,
        timestamp=timestamp,
        access_secret=ACRCLOUD_ACCESS_SECRET,
    )

    url = f"https://{ACRCLOUD_HOST}{http_uri}"

    with open(file_path, "rb") as audio_file:
        sample_bytes = audio_file.read()

    files = {
        "sample": ("sample.mp3", sample_bytes, "audio/mpeg")
    }

    data = {
        "access_key": ACRCLOUD_ACCESS_KEY,
        "sample_bytes": str(len(sample_bytes)),
        "timestamp": timestamp,
        "signature": signature,
        "data_type": data_type,
        "signature_version": signature_version,
    }

    try:
        response = requests.post(url, files=files, data=data, timeout=60)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        print("ACRCloud request failed:", e)
        if hasattr(e, "response") and e.response is not None:
            print("Response text:", e.response.text)
        return None


def print_acrcloud_results(result: dict):
    if result is None:
        print("No response from ACRCloud.")
        return

    print("\nFull ACRCloud response:")
    print(json.dumps(result, indent=2))

    status = result.get("status", {})
    if status.get("code") != 0:
        print("\nNo match found or request not successful.")
        return

    metadata = result.get("metadata", {})
    music_list = metadata.get("music", [])

    if not music_list:
        print("\nNo recognized track found in metadata.music.")
        return

    song = music_list[0]

    print("\n🎵 Song Identified")
    print("----------------------")
    print("Title:", song.get("title"))

    artists = song.get("artists", [])
    if artists:
        print("Artist(s):", ", ".join(a.get("name", "") for a in artists if a.get("name")))

    album = song.get("album", {})
    if album:
        print("Album:", album.get("name"))

    external_metadata = song.get("external_metadata", {})

    spotify = external_metadata.get("spotify", {})
    if spotify:
        track = spotify.get("track", {})
        external_urls = track.get("external_urls", {})
        spotify_url = external_urls.get("spotify")
        if spotify_url:
            print("\nSpotify:")
            print(spotify_url)

    apple_music = external_metadata.get("apple_music", {})
    if apple_music:
        apple_url = apple_music.get("url")
        if apple_url:
            print("\nApple Music:")
            print(apple_url)


def main():
    print("Step 1: Creating 11-second clip...")
    create_clip(
        source_path=SOURCE_FILE,
        output_path=CLIP_FILE,
        start_sec=START_SECONDS,
        duration_sec=DURATION_SECONDS,
    )

    print("Step 2: Sending clip to ACRCloud...")
    result = recognize_song_with_acrcloud(CLIP_FILE)

    print("Step 3: Printing results...")
    print_acrcloud_results(result)


if __name__ == "__main__":
    main()
    