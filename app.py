"""yt-dlp -> ffmpeg -> native Python HTTP upload (Litterbox 1GB / 1h expiry).
FastAPI Server with Async Queue, Interactive Input Controls, & Mobile UI Dashboard.
"""
import asyncio
import os
import re
import subprocess
import time
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


def process_job_sync(jid, source_url):
    job = JOBS[jid]
    out_path = f"/tmp/{jid}.mp4"
    proc = None
    try:
        if job.get("cancelled"):
            raise RuntimeError("Job cancelled by user.")

        job["status"] = "analyzing URL"
        job["log"] = "Extracting media metadata via yt-dlp..."

        opts = {"quiet": True, "no_warnings": True, "noplaylist": True, "skip_download": True}
        if PROXY:
            opts["proxy"] = PROXY
        if YT_COOKIES:
            opts["cookiefile"] = COOKIE_PATH

        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(source_url, download=False)

        if job.get("cancelled"):
            raise RuntimeError("Job cancelled by user.")

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

        clean_title = re.sub(r"[^A-Za-z0-9_\- ]", "", info.get("title") or "")[:60].strip() or "video"
        
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
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
    asyncio.create_task(queue_worker())


class Req(BaseModel):
    url: str = ""
    token: str = ""


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
        "cancelled": False
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
    <input id="u" placeholder="Video URL">
    <button id="inputActionBtn" class="input-action-btn" type="button" onclick="handleInputAction()" title="Paste">
      <svg width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M16 4h2a2 2 0 012 2v14a2 2 0 01-2 2H6a2 2 0 01-2-2V6a2 2 0 012-2h2"></path><rect x="8" y="2" width="8" height="4" rx="1" ry="1"></rect></svg>
    </button>
  </div>
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
  if(!u.value.trim()) return;
  
  if(t.value.trim()) {
    localStorage.setItem('access_token', t.value.trim());
  }

  const r = await fetch('/api/jobs', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({url: u.value.trim(), token: t.value.trim()})
  });
  
  if(r.ok) {
    u.value = '';
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
            <div><span class="badge badge-active">${escapeHtml(j.status)}</span></div>
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
            <div><span class="badge badge-queued">Queue Position #${idx + 1}</span></div>
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
            <div><span class="badge ${bClass}">${escapeHtml(j.status)}</span></div>
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
