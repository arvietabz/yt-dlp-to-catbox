"""
yt-dlp -> ffmpeg -> native Python HTTP upload (Litterbox 1GB / 1h expiry).
FastAPI Server with Tailscale Integration & Mobile UI Dashboard.
File: app.py
"""
import asyncio
import base64
import ipaddress
import os
import re
import subprocess
import time
import urllib.parse
import uuid
import requests
import yt_dlp
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

MAX_BYTES = int(float(os.getenv("MAX_MB", "1000")) * 1_000_000)  # 1 GB target limit
HARD_BYTES = 1_050_000_000                                        # 1.05 GB ceiling
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN", "").strip()
LITTERBOX_URL = "https://litterbox.catbox.moe/resources/internals/api.php"

# Raw PROXY variable read from Render environment
RAW_PROXY = os.getenv("PROXY", "").strip()

# Internal Proxy Bridge configuration for Tailscale Userspace Networking
LOCAL_BRIDGE_PORT = 8888
TS_SOCKS_HOST = "127.0.0.1"
TS_SOCKS_PORT = 1055

REMOTE_PROXY_HOST = ""
REMOTE_PROXY_PORT = 8080
REMOTE_PROXY_USER = ""
REMOTE_PROXY_PASS = ""
IS_TAILSCALE_PROXY = False

if RAW_PROXY:
    parsed_url = RAW_PROXY if "://" in RAW_PROXY else "http://" + RAW_PROXY
    parsed = urllib.parse.urlparse(parsed_url)
    REMOTE_PROXY_HOST = parsed.hostname or ""
    REMOTE_PROXY_PORT = parsed.port or 8080
    REMOTE_PROXY_USER = parsed.username or ""
    REMOTE_PROXY_PASS = parsed.password or ""

    try:
        ip = ipaddress.ip_address(REMOTE_PROXY_HOST)
        if ip in ipaddress.ip_network("100.64.0.0/10"):
            IS_TAILSCALE_PROXY = True
    except ValueError:
        pass

# Determine effective proxy string used by yt-dlp, ffmpeg, and requests
if IS_TAILSCALE_PROXY:
    if REMOTE_PROXY_USER or REMOTE_PROXY_PASS:
        PROXY = f"http://{REMOTE_PROXY_USER}:{REMOTE_PROXY_PASS}@127.0.0.1:{LOCAL_BRIDGE_PORT}"
    else:
        PROXY = f"http://127.0.0.1:{LOCAL_BRIDGE_PORT}"
else:
    PROXY = RAW_PROXY

YT_COOKIES = os.getenv("YT_COOKIES", "").strip()
COOKIE_PATH = "/tmp/cookies.txt"

if YT_COOKIES:
    with open(COOKIE_PATH, "w") as _f:
        _f.write(YT_COOKIES)

app = FastAPI()
JOBS: dict[str, dict] = {}
JOB_QUEUE: asyncio.Queue = asyncio.Queue()


async def handle_bridge_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    """Bridge local connections on 127.0.0.1:8888 through Tailscale SOCKS5 daemon to phone."""
    try:
        ts_reader, ts_writer = await asyncio.open_connection(TS_SOCKS_HOST, TS_SOCKS_PORT)
        
        ts_writer.write(b"\x05\x01\x00")
        await ts_writer.drain()
        socks_init = await ts_reader.readexactly(2)
        if socks_init != b"\x05\x00":
            writer.close()
            return

        try:
            ip_obj = ipaddress.ip_address(REMOTE_PROXY_HOST)
            if ip_obj.version == 4:
                req = b"\x05\x01\x00\x01" + ip_obj.packed + REMOTE_PROXY_PORT.to_bytes(2, "big")
            else:
                req = b"\x05\x01\x00\x04" + ip_obj.packed + REMOTE_PROXY_PORT.to_bytes(2, "big")
        except ValueError:
            host_bytes = REMOTE_PROXY_HOST.encode("utf-8")
            req = b"\x05\x01\x00\x03" + len(host_bytes).to_bytes(1, "big") + host_bytes + REMOTE_PROXY_PORT.to_bytes(2, "big")
            
        ts_writer.write(req)
        await ts_writer.drain()
        
        resp = await ts_reader.read(10)
        if len(resp) < 2 or resp[1] != 0x00:
            writer.close()
            return

        async def pipe(r: asyncio.StreamReader, w: asyncio.StreamWriter):
            try:
                while True:
                    data = await r.read(65536)
                    if not data:
                        break
                    w.write(data)
                    await w.drain()
            except Exception:
                pass
            finally:
                try:
                    w.close()
                except Exception:
                    pass

        await asyncio.gather(
            pipe(reader, ts_writer),
            pipe(ts_reader, writer),
            return_exceptions=True
        )

    except Exception:
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def start_proxy_bridge():
    if IS_TAILSCALE_PROXY and REMOTE_PROXY_HOST:
        server = await asyncio.start_server(
            handle_bridge_client,
            "127.0.0.1",
            LOCAL_BRIDGE_PORT
        )
        print(f"Proxy bridge running on 127.0.0.1:{LOCAL_BRIDGE_PORT} -> Tailscale SOCKS5 -> {REMOTE_PROXY_HOST}:{REMOTE_PROXY_PORT}")
        return server
    return None


@app.get("/api/test-proxy")
def test_proxy():
    if not PROXY:
        return {"status": "error", "message": "PROXY environment variable is empty in Render."}
    
    proxies = {
        "http": PROXY,
        "https": PROXY,
    }
    
    try:
        r = requests.get("https://api.ipify.org?format=json", proxies=proxies, timeout=15)
        return {
            "status": "success",
            "proxy_configured": PROXY,
            "raw_proxy": RAW_PROXY,
            "is_tailscale_proxy": IS_TAILSCALE_PROXY,
            "detected_public_ip": r.json().get("ip"),
            "message": "Proxy is working! Traffic is successfully routing through your phone."
        }
    except requests.exceptions.ProxyError as e:
        return {
            "status": "failed",
            "error_type": "ProxyError (Authentication or Protocol invalid)",
            "proxy_configured": PROXY,
            "raw_proxy": RAW_PROXY,
            "details": str(e)
        }
    except requests.exceptions.ConnectTimeout as e:
        return {
            "status": "failed",
            "error_type": "ConnectTimeout (Render cannot reach phone's IP/Port)",
            "proxy_configured": PROXY,
            "raw_proxy": RAW_PROXY,
            "details": str(e)
        }
    except Exception as e:
        return {
            "status": "failed",
            "error_type": type(e).__name__,
            "proxy_configured": PROXY,
            "raw_proxy": RAW_PROXY,
            "details": str(e)
        }


@app.get("/api/debug-url")
def debug_url(url: str, use_proxy: bool = True):
    job_proxy = PROXY if use_proxy else ""
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
    }
    if job_proxy:
        opts["proxy"] = job_proxy
    if YT_COOKIES:
        opts["cookiefile"] = COOKIE_PATH

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            
        formats = info.get("formats", [])
        parsed_formats = []
        for f in formats:
            parsed_formats.append({
                "format_id": f.get("format_id"),
                "ext": f.get("ext"),
                "height": f.get("height"),
                "vcodec": f.get("vcodec"),
                "acodec": f.get("acodec"),
                "filesize_mb": round((f.get("filesize") or f.get("filesize_approx") or 0) / 1e6, 2),
                "tbr": f.get("tbr"),
                "protocol": f.get("protocol")
            })

        return {
            "status": "success",
            "title": info.get("title"),
            "duration_sec": info.get("duration"),
            "extractor": info.get("extractor"),
            "total_formats_found": len(formats),
            "formats": parsed_formats
        }
    except Exception as e:
        return {
            "status": "error",
            "error_type": type(e).__name__,
            "details": str(e)
        }


def est_size(f, dur):
    s = f.get("filesize") or f.get("filesize_approx")
    if s:
        return s
    br = f.get("tbr") or ((f.get("vbr") or 0) + (f.get("abr") or 0))
    if br:
        d = dur if (dur and dur > 0) else 180
        return int(br * 1000 / 8 * d)
    return 0


def pick(info):
    dur = info.get("duration") or 0
    fmts = [f for f in info.get("formats", []) if f.get("url") and f.get("protocol") != "mhtml"]
    
    vid = []
    aud = []
    prog = []

    for f in fmts:
        v = f.get("vcodec")
        a = f.get("acodec")
        
        # Audio only stream
        if v == "none" and a not in (None, "none"):
            aud.append(f)
        # Video only stream
        elif a == "none" and v not in (None, "none"):
            vid.append(f)
        # Combined stream or streams with null codec metadata (e.g. PornHub MP4/HLS)
        elif v != "none" and a != "none":
            prog.append(f)
        elif (f.get("height") or 0) > 0 or f.get("ext") in ("mp4", "webm", "m3u8", "mov", "flv"):
            prog.append(f)

    aud.sort(key=lambda f: f.get("abr") or f.get("tbr") or 0, reverse=True)

    cands = []
    for p in prog:
        s = est_size(p, dur)
        if 0 <= s <= MAX_BYTES:
            effective_s = s if s > 0 else 50_000_000  # Default ~50MB estimate if unknown
            cands.append((p.get("height") or 0, p.get("tbr") or 0, [p], effective_s))

    for v in vid:
        vs = est_size(v, dur)
        for a in aud:
            as_ = est_size(a, dur)
            tot = vs + as_
            if 0 <= tot <= MAX_BYTES:
                effective_tot = tot if tot > 0 else 50_000_000
                cands.append((v.get("height") or 0, v.get("tbr") or 0, [v, a], effective_tot))
                break

    if cands:
        cands.sort(key=lambda c: (c[0], c[1]), reverse=True)
        best = cands[0]
        return (best[0], best[1]), best[2], best[3]

    if prog:
        prog.sort(key=lambda f: (f.get("height") or 0, f.get("tbr") or 0), reverse=True)
        return ((0, 0), [prog[0]], MAX_BYTES)

    return None


def plan_transcode(info):
    dur = info.get("duration")
    if not dur or dur <= 0:
        dur = 180

    abr = 128
    total_kbps = MAX_BYTES * 0.92 * 8 / dur / 1000
    vbr = int(total_kbps - abr)
    
    target_h = 240
    for h in (1080, 720, 480, 360, 240):
        need = {1080: 2500, 720: 1200, 480: 600, 360: 300, 240: 150}[h]
        if vbr >= need:
            target_h = h
            break
    else:
        vbr = 150

    fmts = [f for f in info.get("formats", []) if f.get("url") and f.get("protocol") != "mhtml"]
    vid = [f for f in fmts if f.get("vcodec") != "none"]
    if not vid:
        return None

    ok = [f for f in vid if (f.get("height") or 0) <= target_h] or vid
    ok.sort(key=lambda f: (f.get("height") or 0, f.get("tbr") or 0), reverse=True)
    src = ok[0]
    chosen = [src]

    if src.get("acodec") in (None, "none"):
        aud = [f for f in fmts if f.get("acodec") not in (None, "none") and f.get("vcodec") in (None, "none")]
        if not aud:
            return None
        aud.sort(key=lambda f: f.get("abr") or 0, reverse=True)
        chosen.append(aud[0])

    scale = target_h if (src.get("height") or 0) > target_h else None
    return chosen, {"vbr": vbr, "abr": abr, "scale": scale}, MAX_BYTES, target_h


def build_ffmpeg_cmd(chosen, out_path, enc=None, extra_headers=None, proxy=None):
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-nostdin"]
    for f in chosen:
        merged_headers = {**(f.get("http_headers") or {}), **(extra_headers or {})}
        hdr = "".join(f"{k}: {v}\r\n" for k, v in merged_headers.items())
        if proxy:
            cmd += ["-http_proxy", proxy]
        cmd += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5"]
        if hdr:
            cmd += ["-headers", hdr]
        cmd += ["-i", f["url"]]

    if len(chosen) == 2:
        cmd += ["-map", "0:v:0", "-map", "1:a:0"]

    acodec = chosen[-1].get("acodec") or ""
    if enc:
        v = enc["vbr"]
        if enc["scale"]:
            cmd += ["-vf", f"scale=-2:{enc['scale']}"]
        cmd += ["-c:v", "libx264", "-preset", os.getenv("X264_PRESET", "veryfast"),
                "-pix_fmt", "yuv420p", "-b:v", f"{v}k", "-maxrate", f"{int(v * 1.1)}k",
                "-bufsize", f"{v * 2}k", "-c:a", "aac", "-b:a", f"{enc['abr']}k"]
    else:
        cmd += ["-c:v", "copy", "-c:a", "copy" if acodec.startswith("mp4a") else "aac"]

    cmd += ["-movflags", "+faststart", out_path]
    return cmd


def process_job_sync(jid, source_url):
    job = JOBS[jid]
    out_path = f"/tmp/{jid}.mp4"
    proc = None
    try:
        if job.get("cancelled"):
            raise RuntimeError("Job cancelled by user.")

        job_proxy = PROXY if job.get("use_proxy") else ""

        custom_headers = {}
        if job.get("referer"):
            custom_headers["Referer"] = job["referer"]
        if job.get("user_agent"):
            custom_headers["User-Agent"] = job["user_agent"]

        choice = None
        enc = None
        info = {}

        if job.get("direct_mode"):
            job["status"] = "direct streaming"
            job["log"] = "Bypassing yt-dlp, passing stream directly to FFmpeg..."
            chosen = [{
                "url": source_url,
                "http_headers": custom_headers,
                "height": 0,
                "acodec": "aac",
                "vcodec": "h264"
            }]
            info = {"title": job.get("custom_title") or "Direct Stream Video"}
            choice = ((0, 0), chosen, MAX_BYTES)
        else:
            job["status"] = "analyzing URL"
            job["log"] = f"Extracting media metadata via yt-dlp {'(via Proxy)' if job_proxy else ''}..."

            opts = {
                "quiet": True,
                "no_warnings": True,
                "noplaylist": True,
                "skip_download": True,
                "http_headers": custom_headers if custom_headers else None
            }
            if job_proxy:
                opts["proxy"] = job_proxy
            if YT_COOKIES:
                opts["cookiefile"] = COOKIE_PATH
            if job.get("force_generic"):
                opts["force_generic_extractor"] = True

            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(source_url, download=False)

            if job.get("cancelled"):
                raise RuntimeError("Job cancelled by user.")

            choice = pick(info)

            if not choice and os.getenv("ALLOW_TRANSCODE", "1") == "1":
                plan = plan_transcode(info)
                if plan:
                    chosen, enc, size, th = plan
                    choice = ((0, 0), chosen, size)
                    job["note"] = f"re-encoding to {min(th, chosen[0].get('height') or th)}p to fit"

        if not choice:
            d = info.get("duration") or 0
            raise RuntimeError(f"No suitable video format found under 1 GB limit (duration {int(d // 60)} min).")

        _, chosen, size = choice
        vf = chosen[0]
        title_text = job.get("custom_title") or info.get("title") or "Unsupported Video Stream"
        
        job.update(
            status="downloading & processing",
            quality=f"{vf.get('height') or 'stream'}p ~{int(size / 1e6)} MB",
            title=title_text
        )
        job["log"] = f"Processing video streams with ffmpeg {'(via Proxy)' if job_proxy else ''}..."

        cmd = build_ffmpeg_cmd(chosen, out_path, enc, extra_headers=custom_headers, proxy=job_proxy)
        proc = subprocess.Popen(cmd, stderr=subprocess.PIPE)

        while proc.poll() is None:
            time.sleep(0.5)
            if job.get("cancelled"):
                proc.kill()
                raise RuntimeError("Job cancelled by user.")
            if os.path.exists(out_path):
                curr_size = os.path.getsize(out_path)
                job["bytes"] = curr_size
                if curr_size > HARD_BYTES:
                    proc.kill()
                    raise RuntimeError("File size exceeded 1 GB ceiling during processing; aborted.")

        if proc.returncode != 0:
            err_msg = proc.stderr.read().decode("utf-8", errors="replace")[-400:]
            raise RuntimeError(f"FFmpeg failed (exit code {proc.returncode}): {err_msg}")

        if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
            raise RuntimeError("FFmpeg completed but produced an empty file.")

        if job.get("cancelled"):
            raise RuntimeError("Job cancelled by user.")

        final_size = os.path.getsize(out_path)
        job.update(status="uploading to litterbox (1h expiry)", total_size=final_size, bytes=final_size)
        job["log"] = f"File processed ({final_size / 1e6:.1f} MB). Dispatching HTTP upload..."

        clean_title = re.sub(r"[^A-Za-z0-9_\- ]", "", title_text)[:60].strip() or "video"
        
        headers = {
            "User-Agent": job.get("user_agent") or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        data = {
            "reqtype": "fileupload",
            "time": "1h"
        }

        with open(out_path, "rb") as f:
            files = {
                "fileToUpload": (f"{clean_title}.mp4", f, "video/mp4")
            }
            r = requests.post(LITTERBOX_URL, data=data, files=files, headers=headers, timeout=(30, 1200))

        if job.get("cancelled"):
            raise RuntimeError("Job cancelled by user.")

        out_url = r.text.strip()
        if not out_url.startswith("http"):
            raise RuntimeError(f"Litterbox response error [HTTP {r.status_code}]: '{out_url[:200]}'")

        job.update(status="done", result_url=out_url, log="Upload completed successfully!")

    except Exception as e:
        if proc and proc.poll() is None:
            proc.kill()

        if job.get("cancelled"):
            job.update(status="cancelled", log="Job cancelled by user.", error="")
        else:
            job.update(status="error", error=str(e)[:400], log=f"Failed: {str(e)[:200]}")
    finally:
        if os.path.exists(out_path):
            try:
                os.remove(out_path)
            except Exception:
                pass


async def queue_worker():
    while True:
        jid = await JOB_QUEUE.get()
        job = JOBS.get(jid)
        if job and not job.get("cancelled") and job["status"] == "queued":
            await asyncio.to_thread(process_job_sync, jid, job["source_url"])
        JOB_QUEUE.task_done()


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


class Req(BaseModel):
    url: str = ""
    token: str = ""
    referer: str = ""
    user_agent: str = ""
    custom_title: str = ""
    direct_mode: bool = False
    force_generic: bool = False
    use_proxy: bool = False


@app.get("/api/jobs")
def list_all_jobs():
    items = []
    for k, v in JOBS.items():
        items.append({"id": k, **v})
    items.sort(key=lambda x: x["t"], reverse=True)
    return items


@app.post("/api/jobs")
async def create(req: Req):
    if ACCESS_TOKEN and req.token != ACCESS_TOKEN:
        raise HTTPException(401, "bad token")
    if not re.match(r"^https?://", req.url):
        raise HTTPException(400, "invalid url")
    
    for k in list(JOBS)[:-30]:
        JOBS.pop(k, None)
        
    jid = uuid.uuid4().hex[:10]
    JOBS[jid] = {
        "source_url": req.url,
        "status": "queued",
        "bytes": 0,
        "log": "Queued for processing...",
        "t": time.time(),
        "cancelled": False,
        "referer": req.referer.strip(),
        "user_agent": req.user_agent.strip(),
        "custom_title": req.custom_title.strip(),
        "direct_mode": req.direct_mode,
        "force_generic": req.force_generic,
        "use_proxy": req.use_proxy
    }
    await JOB_QUEUE.put(jid)
    return {"id": jid}


@app.post("/api/jobs/{jid}/cancel")
def cancel_job(jid: str):
    if jid not in JOBS:
        raise HTTPException(404, "Job not found")
    job = JOBS[jid]
    if job["status"] in ("done", "error", "cancelled"):
        return {"status": job["status"], "message": "Job already finished"}
    job["cancelled"] = True
    job["status"] = "cancelled"
    job["log"] = "Job cancelled by user."
    return {"status": "cancelled"}


@app.delete("/api/jobs/{jid}")
def delete_job(jid: str):
    if jid in JOBS:
        JOBS.pop(jid, None)
        return {"status": "deleted"}
    raise HTTPException(404, "Job not found")


@app.post("/api/jobs/clear-history")
def clear_history():
    to_remove = [k for k, v in JOBS.items() if v["status"] in ("done", "error", "cancelled")]
    for k in to_remove:
        JOBS.pop(k, None)
    return {"status": "cleared", "count": len(to_remove)}


@app.get("/api/jobs/{jid}")
def get_job(jid: str):
    if jid not in JOBS:
        raise HTTPException(404)
    return JOBS[jid]


@app.get("/", response_class=HTMLResponse)
def home():
    return """<!doctype html><meta name=viewport content="width=device-width,initial-scale=1">
<title>yt → litterbox</title>
<style>
  body{font:15px system-ui,-apple-system,sans-serif;max-width:540px;margin:1.5rem auto;padding:0 1rem;background:#f9f9f9;color:#222}
  input,button{width:100%;padding:.75rem;margin:.3rem 0;font-size:1rem;box-sizing:border-box;border-radius:6px;border:1px solid #ccc}
  button{background:#0066cc;color:#fff;font-weight:600;border:none;cursor:pointer}
  button:hover{background:#0052a3}
  
  .input-wrapper {
    position: relative;
    width: 100%;
  }
  .input-wrapper input {
    padding-right: 42px !important;
  }
  .input-action-btn {
    position: absolute;
    right: 8px;
    top: 50%;
    transform: translateY(-50%);
    background: transparent !important;
    border: none !important;
    padding: 6px !important;
    margin: 0 !important;
    width: auto !important;
    cursor: pointer;
    color: #888;
    display: flex;
    align-items: center;
    justify-content: center;
    border-radius: 4px;
  }
  .input-action-btn:hover {
    color: #222;
  }

  details {
    margin: .6rem 0;
    border: 1px dashed #bbb;
    border-radius: 6px;
    padding: .5rem .8rem;
    background: #fafafa;
  }
  summary {
    font-weight: 600;
    font-size: .85rem;
    color: #444;
    cursor: pointer;
  }
  .adv-option {
    margin-top: .4rem;
  }
  .adv-option input {
    padding: .5rem;
    font-size: .85rem;
  }
  .checkbox-label {
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: .85rem;
    color: #333;
    margin: .4rem 0;
    cursor: pointer;
  }
  .checkbox-label input {
    width: auto !important;
    margin: 0 !important;
  }

  .swipe-container {
    position: relative;
    overflow: hidden;
    margin: .8rem 0;
    border-radius: 8px;
  }
  .swipe-action-bg {
    position: absolute;
    top: 0; right: 0; bottom: 0; left: 0;
    background: #d9534f;
    display: flex;
    align-items: center;
    justify-content: flex-end;
    padding-right: 20px;
    border-radius: 8px;
    z-index: 1;
  }
  .swipe-action-btn {
    background: transparent !important;
    border: none !important;
    color: white !important;
    padding: 10px !important;
    margin: 0 !important;
    width: auto !important;
    cursor: pointer;
    display: flex;
    align-items: center;
  }

  .card{
    position: relative;
    z-index: 2;
    background:#fff;
    border:1px solid #e0e0e0;
    border-radius:8px;
    padding:.8rem 1rem;
    box-shadow:0 1px 3px rgba(0,0,0,0.05);
    transition: transform 0.15s ease-out;
  }
  .card.active{border-left:5px solid #0066cc}
  .card.queued{border-left:5px solid #f0ad4e}
  .card.done{border-left:5px solid #5cb85c}
  .card.error,.card.cancelled{border-left:5px solid #d9534f}
  
  .badge{display:inline-block;padding:.2rem .5rem;font-size:.75rem;font-weight:bold;border-radius:4px;text-transform:uppercase}
  .badge-active{background:#e6f2ff;color:#0066cc}
  .badge-queued{background:#fef5e7;color:#f0ad4e}
  .badge-done{background:#eafaf1;color:#27ae60}
  .badge-error{background:#fadbd8;color:#c0392b}
  
  .section-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin: 1.2rem 0 .4rem 0;
    border-bottom: 1px solid #ddd;
    padding-bottom: .3rem;
  }
  .section-title{font-size:1.1rem;font-weight:bold;color:#444}
  
  .clear-btn {
    width: auto !important;
    padding: .25rem .6rem !important;
    font-size: .8rem !important;
    background: transparent !important;
    color: #888 !important;
    border: 1px solid #ccc !important;
    border-radius: 4px !important;
    margin: 0 !important;
  }
  .clear-btn:hover {
    background: #fee !important;
    color: #c0392b !important;
    border-color: #f5c6cb !important;
  }

  button.cancel{background:#d9534f;color:#fff;padding:.4rem .8rem;font-size:.85rem;width:auto;margin-top:.4rem}
  button.cancel:hover{background:#c9302c}

  .url-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    margin-top: .4rem;
  }
  .copy-btn {
    width: auto !important;
    padding: 4px 8px !important;
    margin: 0 !important;
    background: #f0f0f0 !important;
    color: #333 !important;
    border: 1px solid #ccc !important;
    border-radius: 4px !important;
    display: flex;
    align-items: center;
    justify-content: center;
  }
  .copy-btn:hover {
    background: #e0e0e0 !important;
  }
  
  a{color:#0066cc;text-decoration:none;word-break:break-all}
  a:hover{text-decoration:underline}
</style>

<h2>Video → Litterbox (1h Expiry / 1 GB Limit)</h2>
<div class="card" style="z-index:1">
  <div class="input-wrapper">
    <input id="u" placeholder="Video URL or .m3u8 Stream">
    <button id="inputActionBtn" class="input-action-btn" type="button" onclick="handleInputAction()" title="Paste">
      <svg width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M16 4h2a2 2 0 012 2v14a2 2 0 01-2 2H6a2 2 0 01-2-2V6a2 2 0 012-2h2"></path><rect x="8" y="2" width="8" height="4" rx="1" ry="1"></rect></svg>
    </button>
  </div>
  
  <label class="checkbox-label" style="font-weight: 600; margin-top: .6rem;">
    <input type="checkbox" id="useProxy">
    🌐 Use Mobile Residential Proxy (Tailscale)
  </label>

  <details>
    <summary>⚡ Advanced / Unsupported Site Controls</summary>
    <div class="adv-option">
      <input id="ref" placeholder="Referer URL (e.g. https://site.com/embed)">
      <input id="ua" placeholder="User-Agent Header (optional)">
      <input id="ctitle" placeholder="Custom Output Filename (optional)">
      <label class="checkbox-label">
        <input type="checkbox" id="directMode">
        Direct Stream / M3U8 (Bypass yt-dlp)
      </label>
      <label class="checkbox-label">
        <input type="checkbox" id="forceGeneric">
        Force Generic Extractor (--force-generic-extractor)
      </label>
    </div>
  </details>

  <input id="t" placeholder="Access token (if set)" type="password">
  <button onclick="submitJob()">Upload to Queue</button>
</div>

<div id="queueContainer"></div>

<script>
function escapeHtml(str) {
  return (str || '').replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

const openCards = new Set();
let touchState = {};

const pasteIcon = `<svg width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M16 4h2a2 2 0 012 2v14a2 2 0 01-2 2H6a2 2 0 01-2-2V6a2 2 0 012-2h2"></path><rect x="8" y="2" width="8" height="4" rx="1" ry="1"></rect></svg>`;
const clearIcon = `<svg width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M18 6L6 18M6 6l12 12"></path></svg>`;

function updateInputActionIcon() {
  const u = document.getElementById('u');
  const btn = document.getElementById('inputActionBtn');
  if (!u || !btn) return;
  if (u.value.trim() !== '') {
    btn.innerHTML = clearIcon;
    btn.title = 'Clear URL';
  } else {
    btn.innerHTML = pasteIcon;
    btn.title = 'Paste from Clipboard';
  }
}

async function handleInputAction() {
  const u = document.getElementById('u');
  if (!u) return;
  if (u.value.trim() !== '') {
    u.value = '';
    u.focus();
    updateInputActionIcon();
  } else {
    try {
      const text = await navigator.clipboard.readText();
      if (text) {
        u.value = text.trim();
        updateInputActionIcon();
      }
    } catch (err) {
      alert('Unable to read clipboard. Please grant clipboard permission.');
    }
  }
}

window.addEventListener('DOMContentLoaded', () => {
  const urlInput = document.getElementById('u');
  const tokenInput = document.getElementById('t');
  
  const savedToken = localStorage.getItem('access_token');
  if(savedToken) tokenInput.value = savedToken;
  
  tokenInput.addEventListener('input', () => {
    localStorage.setItem('access_token', tokenInput.value.trim());
  });

  urlInput.addEventListener('input', updateInputActionIcon);
  updateInputActionIcon();

  fetchQueue();
  setInterval(fetchQueue, 1500);
});

async function submitJob() {
  const u = document.getElementById('u');
  const t = document.getElementById('t');
  const ref = document.getElementById('ref');
  const ua = document.getElementById('ua');
  const ctitle = document.getElementById('ctitle');
  const directMode = document.getElementById('directMode');
  const forceGeneric = document.getElementById('forceGeneric');
  const useProxy = document.getElementById('useProxy');

  if(!u.value.trim()) return;
  
  if(t.value.trim()) {
    localStorage.setItem('access_token', t.value.trim());
  }

  const payload = {
    url: u.value.trim(),
    token: t.value.trim(),
    referer: ref.value.trim(),
    user_agent: ua.value.trim(),
    custom_title: ctitle.value.trim(),
    direct_mode: directMode.checked,
    force_generic: forceGeneric.checked,
    use_proxy: useProxy.checked
  };

  const r = await fetch('/api/jobs', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload)
  });
  
  if(r.ok) {
    u.value = '';
    ref.value = '';
    ua.value = '';
    ctitle.value = '';
    directMode.checked = false;
    forceGeneric.checked = false;
    useProxy.checked = false;
    updateInputActionIcon();
    fetchQueue();
  } else {
    alert('Failed to submit job: HTTP ' + r.status);
  }
}

async function cancelJob(jid) {
  await fetch('/api/jobs/' + jid + '/cancel', {method: 'POST'});
  openCards.delete(jid);
  fetchQueue();
}

async function deleteCard(jid) {
  openCards.delete(jid);
  await fetch('/api/jobs/' + jid, {method: 'DELETE'});
  fetchQueue();
}

async function clearAllHistory() {
  await fetch('/api/jobs/clear-history', {method: 'POST'});
  openCards.clear();
  fetchQueue();
}

function copyToClipboard(text, btn) {
  navigator.clipboard.writeText(text).then(() => {
    const origHTML = btn.innerHTML;
    btn.innerHTML = '<span style="font-size:11px;color:#27ae60">✓ Copied</span>';
    setTimeout(() => btn.innerHTML = origHTML, 1500);
  });
}

function handleTouchStart(e, jid) {
  const isAlreadyOpen = openCards.has(jid);
  touchState[jid] = { 
    startX: e.touches[0].clientX, 
    currentX: isAlreadyOpen ? -70 : 0,
    isAlreadyOpen 
  };
}

function handleTouchMove(e, jid) {
  if (!touchState[jid]) return;
  const deltaX = e.touches[0].clientX - touchState[jid].startX;
  let newX = (touchState[jid].isAlreadyOpen ? -70 : 0) + deltaX;
  if (newX > 0) newX = 0;
  if (newX < -110) newX = -110;
  
  touchState[jid].currentX = newX;
  const cardEl = document.getElementById('card-el-' + jid);
  if (cardEl) cardEl.style.transform = `translateX(${newX}px)`;
}

function handleTouchEnd(e, jid) {
  if (!touchState[jid]) return;
  const finalX = touchState[jid].currentX;
  const cardEl = document.getElementById('card-el-' + jid);
  
  if (cardEl) {
    if (finalX < -35) {
      cardEl.style.transform = 'translateX(-70px)';
      openCards.add(jid);
    } else {
      cardEl.style.transform = 'translateX(0px)';
      openCards.delete(jid);
    }
  }
  delete touchState[jid];
}

async function fetchQueue() {
  try {
    const res = await fetch('/api/jobs');
    if(!res.ok) return;
    const jobs = await res.json();
    
    const active = jobs.filter(j => !['queued', 'done', 'error', 'cancelled'].includes(j.status));
    const queued = jobs.filter(j => j.status === 'queued');
    const finished = jobs.filter(j => ['done', 'error', 'cancelled'].includes(j.status));
    
    let html = '';
    
    if(active.length > 0) {
      html += '<div class="section-header"><div class="section-title">Currently Processing</div></div>';
      active.forEach(j => {
        const isOpen = openCards.has(j.id);
        const transformStyle = isOpen ? 'transform: translateX(-70px);' : '';
        const sizeStr = j.total_size ? ((j.bytes/1e6).toFixed(1) + '/' + (j.total_size/1e6).toFixed(1) + ' MB') : ((j.bytes/1e6).toFixed(1) + ' MB');
        
        html += `<div class="swipe-container">
          <div class="swipe-action-bg">
            <button class="swipe-action-btn" onclick="deleteCard('${j.id}')">
              <svg width="22" height="22" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M3 6h18M19 6v14a2 2 0 01-2 2H7a2 2 0 01-2-2V6m3 0V4a2 2 0 012-2h4a2 2 0 012 2v2M10 11v6M14 11v6"/></svg>
            </button>
          </div>
          <div class="card active" id="card-el-${j.id}" style="${transformStyle}" ontouchstart="handleTouchStart(event, '${j.id}')" ontouchmove="handleTouchMove(event, '${j.id}')" ontouchend="handleTouchEnd(event, '${j.id}')">
            <div>
              <span class="badge badge-active">${escapeHtml(j.status)}</span>
              ${j.use_proxy ? '<span class="badge badge-queued" style="margin-left:4px">PROXY ON</span>' : ''}
            </div>
            <div style="margin-top:.4rem"><b>${escapeHtml(j.title || j.source_url)}</b></div>
            ${j.quality ? '<div>Quality: ' + escapeHtml(j.quality) + '</div>' : ''}
            <div>Size: ${sizeStr}</div>
            ${j.log ? '<div style="color:#666;font-size:.85rem;margin-top:.3rem">' + escapeHtml(j.log) + '</div>' : ''}
            <button class="cancel" onclick="cancelJob('${j.id}')">Cancel Job</button>
          </div>
        </div>`;
      });
    }
    
    if(queued.length > 0) {
      html += '<div class="section-header"><div class="section-title">Pending Queue (' + queued.length + ')</div></div>';
      queued.forEach((j, idx) => {
        const isOpen = openCards.has(j.id);
        const transformStyle = isOpen ? 'transform: translateX(-70px);' : '';
        
        html += `<div class="swipe-container">
          <div class="swipe-action-bg">
            <button class="swipe-action-btn" onclick="deleteCard('${j.id}')">
              <svg width="22" height="22" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M3 6h18M19 6v14a2 2 0 01-2 2H7a2 2 0 01-2-2V6m3 0V4a2 2 0 012-2h4a2 2 0 012 2v2M10 11v6M14 11v6"/></svg>
            </button>
          </div>
          <div class="card queued" id="card-el-${j.id}" style="${transformStyle}" ontouchstart="handleTouchStart(event, '${j.id}')" ontouchmove="handleTouchMove(event, '${j.id}')" ontouchend="handleTouchEnd(event, '${j.id}')">
            <div>
              <span class="badge badge-queued">Queue Position #${idx + 1}</span>
              ${j.use_proxy ? '<span class="badge badge-queued" style="margin-left:4px">PROXY ON</span>' : ''}
            </div>
            <div style="margin-top:.4rem;word-break:break-all"><b>${escapeHtml(j.source_url)}</b></div>
            <button class="cancel" onclick="cancelJob('${j.id}')">Remove from Queue</button>
          </div>
        </div>`;
      });
    }
    
    if(finished.length > 0) {
      html += `<div class="section-header">
        <div class="section-title">Recent Activity</div>
        <button class="clear-btn" onclick="clearAllHistory()">Clear All</button>
      </div>`;
      finished.slice(0, 10).forEach(j => {
        const isOpen = openCards.has(j.id);
        const transformStyle = isOpen ? 'transform: translateX(-70px);' : '';
        const bClass = j.status === 'done' ? 'badge-done' : 'badge-error';
        const copyBtn = j.result_url ? `<button class="copy-btn" onclick="copyToClipboard('${escapeHtml(j.result_url)}', this)" title="Copy Link">
          <svg width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1"/></svg>
        </button>` : '';

        html += `<div class="swipe-container">
          <div class="swipe-action-bg">
            <button class="swipe-action-btn" onclick="deleteCard('${j.id}')">
              <svg width="22" height="22" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M3 6h18M19 6v14a2 2 0 01-2 2H7a2 2 0 01-2-2V6m3 0V4a2 2 0 012-2h4a2 2 0 012 2v2M10 11v6M14 11v6"/></svg>
            </button>
          </div>
          <div class="card ${j.status}" id="card-el-${j.id}" style="${transformStyle}" ontouchstart="handleTouchStart(event, '${j.id}')" ontouchmove="handleTouchMove(event, '${j.id}')" ontouchend="handleTouchEnd(event, '${j.id}')">
            <div>
              <span class="badge ${bClass}">${escapeHtml(j.status)}</span>
              ${j.use_proxy ? '<span class="badge badge-queued" style="margin-left:4px">PROXY ON</span>' : ''}
            </div>
            <div style="margin-top:.3rem"><b>${escapeHtml(j.title || j.source_url)}</b></div>
            ${j.result_url ? '<div class="url-row"><a href="' + escapeHtml(j.result_url) + '" target="_blank">' + escapeHtml(j.result_url) + '</a>' + copyBtn + '</div>' : ''}
            ${j.error ? '<div style="color:#c0392b;font-size:.85rem;margin-top:.3rem">' + escapeHtml(j.error) + '</div>' : ''}
          </div>
        </div>`;
      });
    }
    
    if(jobs.length === 0) {
      html = '<div style="text-align:center;color:#888;margin:2rem 0">No active or queued jobs.</div>';
    }
    
    document.getElementById('queueContainer').innerHTML = html;
  } catch(e) {}
}
</script>"""
