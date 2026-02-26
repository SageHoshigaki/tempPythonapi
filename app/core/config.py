import os
from dotenv import load_dotenv

load_dotenv()

INIT_UPLOAD_URL = os.getenv("INIT_UPLOAD_URL", "").strip()
PROCESS_VIDEO_URL = os.getenv("PROCESS_VIDEO_URL", "").strip()
HTTP_TIMEOUT_SECONDS = float(os.getenv("HTTP_TIMEOUT_SECONDS", "60"))

KEEP_DEBUG_FILES = os.getenv("KEEP_DEBUG_FILES", "0").strip() in (
    "1",
    "true",
    "True",
    "yes",
    "YES",
)

AUDD_API_URL = "https://api.audd.io/"
AUDD_API_TOKEN = os.getenv("AUDD_API_TOKEN", "").strip()

# Audio targets
TARGET_SR = 44100
TARGET_CHANNELS = 1
SAMPLE_WIDTH_BYTES = 2  # s16
WINDOW_SECONDS = 12.0