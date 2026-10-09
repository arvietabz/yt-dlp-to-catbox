import asyncio
import yt_dlp

JOBS: dict[str, dict] = {}
JOB_QUEUE: asyncio.Queue = asyncio.Queue()

CURRENT_YTDLP_VERSION = yt_dlp.version.__version__