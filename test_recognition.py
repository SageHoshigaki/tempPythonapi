import os
import json
import requests
from dotenv import load_dotenv
from pydub import AudioSegment

load_dotenv()

AUDD_API_TOKEN = os.getenv("AUDD_API_TOKEN")
if not AUDD_API_TOKEN:
    raise ValueError("AUDD_API_TOKEN not found in .env")

SOURCE_FILE = "flick.mp3"
CLIP_FILE = "flick_20s_clip.mp3"
START_SEC = 40
DURATION_SECONDS = 20
EXPECTED_AUDIO_ID = 1001  # optional, remove check if unknown


def create_clip(source_path: str, output_path: str, start_sec: int, duration_sec: int):
    if not os.path.exists(source_path):
        raise FileNotFoundError(f"Source file not found: {source_path}")

    audio = AudioSegment.from_file(source_path)
    start_ms = start_sec * 1000
    end_ms = (start_sec + duration_sec) * 1000

    if start_ms >= len(audio):
        raise ValueError("Start time is beyond the length of the audio file.")

    clip = audio[start_ms:end_ms]
    clip.export(output_path, format="mp3", bitrate="320k")


def recognize_song(file_path: str):
    url = "https://api.audd.io/"

    with open(file_path, "rb") as audio_file:
        files = {"file": audio_file}
        data = {
            "api_token": AUDD_API_TOKEN,
        }

        response = requests.post(url, data=data, files=files, timeout=120)
        response.raise_for_status()
        return response.json()


def main():
    print(f"Creating {DURATION_SECONDS}-second clip from {SOURCE_FILE}...")
    create_clip(SOURCE_FILE, CLIP_FILE, START_SEC, DURATION_SECONDS)
    print(f"Created: {CLIP_FILE} ({os.path.getsize(CLIP_FILE)} bytes)")

    result = recognize_song(CLIP_FILE)
    print("\nFull response:")
    print(json.dumps(result, indent=2))

    if result.get("status") != "success" or not result.get("result"):
        print("\nNo match found.")
        return

    match = result["result"]
    print("\nMatch found.")

    returned_audio_id = match.get("audio_id")
    if returned_audio_id is not None:
        print("Returned audio_id:", returned_audio_id)
        if str(returned_audio_id) == str(EXPECTED_AUDIO_ID):
            print("✅ This matches your uploaded flick.mp3")
        else:
            print("⚠️ It matched, but not the expected audio_id.")
    else:
        print("No audio_id returned in the result.")


if __name__ == "__main__":
    main()