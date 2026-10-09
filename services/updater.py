import asyncio
import datetime
import importlib
import yt_dlp
import state

def normalize_ver(v: str) -> str:
    if not v:
        return ""
    return ".".join(str(int(part)) if part.isdigit() else part for part in v.strip().lstrip("v").split("."))

async def perform_ytdlp_update():
    proc = await asyncio.create_subprocess_exec(
        "pip", "install", "--upgrade", "yt-dlp[default,curl-cffi]",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await proc.communicate()
    importlib.reload(yt_dlp)
    state.CURRENT_YTDLP_VERSION = yt_dlp.version.__version__
    return stdout.decode("utf-8", errors="replace")[-300:]

async def ytdlp_auto_updater_loop():
    while True:
        now = datetime.datetime.utcnow()
        next_midnight = (now + datetime.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        seconds_until_midnight = (next_midnight - now).total_seconds()
        
        await asyncio.sleep(seconds_until_midnight)

        try:
            print("[Auto-Updater] Checking for yt-dlp updates at midnight UTC...")
            await perform_ytdlp_update()
            print(f"[Auto-Updater] yt-dlp updated/verified to version: v{state.CURRENT_YTDLP_VERSION}")
        except Exception as e:
            print(f"[Auto-Updater] Midnight update failed: {e}")