import asyncio
import os
import subprocess
from fastapi import FastAPI
from config import IS_TAILSCALE_PROXY
from services.bridge import start_proxy_bridge
from services.processor import queue_worker
from services.updater import ytdlp_auto_updater_loop
from routers import jobs, system, ui

app = FastAPI(title="yt-dlp to Catbox/Litterbox Bridge")

@app.on_event("startup")
async def startup_event():
    ts_authkey = os.getenv("TAILSCALE_AUTHKEY", "").strip()
    if ts_authkey:
        print("Starting Tailscale user-space daemon...")
        try:
            subprocess.Popen([
                "tailscaled", 
                "--tun=userspace-networking",
                "--socks5-server=localhost:1055"
            ])
            await asyncio.sleep(2)
            subprocess.Popen([
                "tailscale", 
                "up", 
                f"--authkey={ts_authkey}", 
                "--hostname=render-fastapi"
            ])
            print("Tailscale node registered successfully!")
            await asyncio.sleep(2)
        except Exception as e:
            print(f"Tailscale startup failed: {e}")

    if IS_TAILSCALE_PROXY:
        await start_proxy_bridge()

    asyncio.create_task(queue_worker())
    asyncio.create_task(ytdlp_auto_updater_loop())

# Register routers
app.include_router(jobs.router)
app.include_router(system.router)
app.include_router(ui.router)