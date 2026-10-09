"""yt-dlp -> ffmpeg -> curl (Litterbox 1GB / 1h expiry).
FastAPI Server with Async Queue & Native cURL Uploads.
"""
import asyncio
import os
import re
import subprocess
import time
import uuid
import yt_dlp
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

MAX_BYTES = int(float(os.getenv("MAX_MB", "1000")) * 1_000_000)  # 1 GB target limit
HARD_BYTES = 1_050_000_000                                        # 1.05 GB ceiling
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN", "").strip()
LITTERBOX_URL = "https://litterbox.catbox.moe/resources/internals/api.php"
PROXY = os.getenv("PROXY", "").strip()
YT_COOKIES = os.getenv("YT_COOKIES", "").strip()
COOKIE_PATH = "/tmp/cookies.txt"

if YT_COOKIES:
    with open(COOKIE_PATH, "w") as _f:
        _f.write(YT_COOKIES)

app = FastAPI()
JOBS: dict[str, dict] = {}
JOB_QUEUE: asyncio.Queue = asyncio.Queue()


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
    
    vid = [f for f in fmts if f.get("vcodec") not in (None, "none") and f.get("acodec") in (None, "none")]
    aud = [f for f in fmts if f.get("acodec") not in (None, "none") and f.get("vcodec") in (None, "none")]
    prog = [f for f in fmts if f.get("vcodec") not in (None, "none") and f.get("acodec") not in (None, "none")]
    
    aud.sort(key=lambda f: f.get("abr") or f.get("tbr") or 0, reverse=True)

    cands = []
    for p in prog:
        s = est_size(p, dur)
        if 0 < s <= MAX_BYTES:
            cands.append((p.get("height") or 0, p.get("tbr") or 0, [p], s))

    for v in vid:
        vs = est_size(v, dur)
        for a in aud:
            as_ = est_size(a, dur)
            tot = vs + as_
            if 0 < tot <= MAX_BYTES:
                cands.append((v.get("height") or 0, v.get("tbr") or 0, [v, a], tot))
                break

    if cands:
        cands.sort(key=lambda c: (c[0], c[1]), reverse=True)
        best = cands[0]
        return (best[0], best[1]), best[2], best[3]

    low_prog = [p for p in prog if (p.get("height") or 0) <= 1080]
    if low_prog:
        low_prog.sort(key=lambda f: f.get("height") or 0)
        return ((0, 0), [low_prog[0]], MAX_BYTES)

    low_vid = [v for v in vid if (v.get("height") or 0) <= 1080]
    if low_vid:
        low_vid.sort(key=lambda f: f.get("height") or 0)
        chosen = [low_vid[0]]
        if aud:
            chosen.append(aud[-1])
        return ((0, 0), chosen, MAX_BYTES)

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
    vid = [f for f in fmts if f.get("vcodec") not in (None, "none")]
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


def build_ffmpeg_cmd(chosen, out_path, enc=None):
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-nostdin"]
    for f in chosen:
        hdr = "".join(f"{k}: {v}\r\n" for k, v in (f.get("http_headers") or {}).items())
        if PROXY:
            cmd += ["-http_proxy", PROXY]
        cmd += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5",
                "-headers", hdr, "-i", f["url"]]

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


def process_job_sync(jid, url):
    job = JOBS[jid]
    out_path = f"/tmp/{jid}.mp4"
    proc = None
    try:
        job["status"] = "analyzing URL"
        job["log"] = "Extracting media metadata via yt-dlp..."

        opts = {"quiet": True, "no_warnings": True, "noplaylist": True, "skip_download": True}
        if PROXY:
            opts["proxy"] = PROXY
        if YT_COOKIES:
            opts["cookiefile"] = COOKIE_PATH

        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)

        choice = pick(info)
        enc = None

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
        job.update(
            status="downloading & processing",
            quality=f"{vf.get('height') or 'unknown'}p ~{int(size / 1e6)} MB",
            title=info.get("title")
        )
        job["log"] = "Processing video streams with ffmpeg..."

        cmd = build_ffmpeg_cmd(chosen, out_path, enc)
        proc = subprocess.Popen(cmd, stderr=subprocess.PIPE)

        while proc.poll() is None:
            time.sleep(0.5)
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

        final_size = os.path.getsize(out_path)
        job.update(status="uploading to litterbox (1h expiry)", total_size=final_size, bytes=final_size)
        job["log"] = f"File processed ({final_size / 1e6:.1f} MB). Dispatching cURL upload..."

        clean_title = re.sub(r"[^A-Za-z0-9_\- ]", "", info.get("title") or "")[:60].strip() or "video"
        upload_cmd = [
            "curl", "-s", "-S",
            "-A", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "-F", "reqtype=fileupload",
            "-F", "time=1h",
            "-F", f"fileToUpload=@{out_path};filename={clean_title}.mp4",
            LITTERBOX_URL
        ]

        curl_proc = subprocess.run(upload_cmd, capture_output=True, text=True, timeout=1200)

        if curl_proc.returncode != 0:
            raise RuntimeError(f"cURL upload failed (code {curl_proc.returncode}): {curl_proc.stderr}")

        out_url = curl_proc.stdout.strip()
        if not out_url.startswith("http"):
            raise RuntimeError(f"Litterbox response error: '{out_url[:200]}'")

        job.update(status="done", url=out_url, log="Upload completed successfully!")

    except Exception as e:
        if proc and proc.poll() is None:
            proc.kill()
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
        if job and job["status"] == "queued":
            await asyncio.to_thread(process_job_sync, jid, job["url"])
        JOB_QUEUE.task_done()


@app.on_event("startup")
async def startup_event():
    asyncio.create_task(queue_worker())


class Req(BaseModel):
    url: str
    token: str = ""


@app.post("/api/jobs")
async def create(req: Req):
    if ACCESS_TOKEN and req.token != ACCESS_TOKEN:
        raise HTTPException(401, "bad token")
    if not re.match(r"^https?://", req.url):
        raise HTTPException(400, "invalid url")
    for k in list(JOBS)[:-30]:
        JOBS.pop(k, None)
    jid = uuid.uuid4().hex[:10]
    JOBS[jid] = {"status": "queued", "bytes": 0, "log": "Queued for processing...", "t": time.time()}
    await JOB_QUEUE.put(jid)
    return {"id": jid}


@app.get("/api/jobs/{jid}")
def status(jid: str):
    if jid not in JOBS:
        raise HTTPException(404)
    return JOBS[jid]


@app.get("/", response_class=HTMLResponse)
def home():
    return """<!doctype html><meta name=viewport content="width=device-width,initial-scale=1">
<title>yt → litterbox</title>
<style>body{font:16px system-ui;max-width:520px;margin:2rem auto;padding:0 1rem}
input,button{width:100%;padding:.8rem;margin:.3rem 0;font-size:1rem;box-sizing:border-box}
pre{white-space:pre-wrap;word-break:break-all}</style>
<h2>Video → Litterbox (1h Expiry / 1 GB Limit)</h2>
<input id=u placeholder="Video URL"><input id=t placeholder="Access token (if set)" type=password>
<button onclick=go()>Upload</button><pre id=o></pre>
<script>
async function go(){const o=document.getElementById('o');o.textContent='Starting…';
const r=await fetch('/api/jobs',{method:'POST',headers:{'Content-Type':'application/json'},
body:JSON.stringify({url:u.value,token:t.value})});
if(!r.ok){o.textContent='Error '+r.status;return}
const {id}=await r.json();
const i=setInterval(async()=>{const j=await (await fetch('/api/jobs/'+id)).json();
const sizeStr = j.total_size ? ((j.bytes/1e6).toFixed(1) + '/' + (j.total_size/1e6).toFixed(1) + ' MB') : ((j.bytes/1e6).toFixed(1) + ' MB');
o.textContent='Status: '+j.status+(j.quality?'\\nQuality: '+j.quality:'')+'\\nSize: '+sizeStr
+(j.log?'\\nLog: '+j.log:'')+(j.note?'\\nNote: '+j.note:'')+(j.error?'\\nError: '+j.error:'')+(j.url?'\\n\\nURL: '+j.url:'');
if(j.status=='done'||j.status=='error')clearInterval(i)},1500)}
</script>"""
