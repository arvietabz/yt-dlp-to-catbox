"""yt-dlp -> ffmpeg -> curl (Litterbox 1GB / 1h expiry).
FastAPI Server with Async Queue, Native cURL Uploads, Global State & UI Queue Dashboard.
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


def process_job_sync(jid, source_url):
    job = JOBS[jid]
    out_path = f"/tmp/{jid}.mp4"
    proc = None
    curl_proc = None
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

        curl_proc = subprocess.Popen(upload_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        while curl_proc.poll() is None:
            time.sleep(0.5)
            if job.get("cancelled"):
                curl_proc.kill()
                raise RuntimeError("Job cancelled by user.")

        out_url, curl_err = curl_proc.communicate()

        if curl_proc.returncode != 0:
            raise RuntimeError(f"cURL upload failed (code {curl_proc.returncode}): {curl_err}")

        out_url = out_url.strip()
        if not out_url.startswith("http"):
            raise RuntimeError(f"Litterbox response error: '{out_url[:200]}'")

        if job.get("cancelled"):
            raise RuntimeError("Job cancelled by user.")

        job.update(status="done", result_url=out_url, log="Upload completed successfully!")

    except Exception as e:
        if proc and proc.poll() is None:
            proc.kill()
        if curl_proc and curl_proc.poll() is None:
            curl_proc.kill()

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
    
    # Retain up to 30 recent jobs in history
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
  button.cancel{background:#d9534f;color:#fff;padding:.4rem .8rem;font-size:.85rem;width:auto;margin-top:.4rem}
  button.cancel:hover{background:#c9302c}
  .card{background:#fff;border:1px solid #e0e0e0;border-radius:8px;padding:.8rem 1rem;margin:.8rem 0;box-shadow:0 1px 3px rgba(0,0,0,0.05)}
  .card.active{border-left:5px solid #0066cc}
  .card.queued{border-left:5px solid #f0ad4e}
  .card.done{border-left:5px solid #5cb85c}
  .card.error,.card.cancelled{border-left:5px solid #d9534f}
  .badge{display:inline-block;padding:.2rem .5rem;font-size:.75rem;font-weight:bold;border-radius:4px;text-transform:uppercase}
  .badge-active{background:#e6f2ff;color:#0066cc}
  .badge-queued{background:#fef5e7;color:#f0ad4e}
  .badge-done{background:#eafaf1;color:#27ae60}
  .badge-error{background:#fadbd8;color:#c0392b}
  .section-title{font-size:1.1rem;margin:1.2rem 0 .4rem 0;color:#444;border-bottom:1px solid #ddd;padding-bottom:.3rem}
  pre{white-space:pre-wrap;word-break:break-all;font-size:.85rem;background:#f0f0f0;padding:.5rem;border-radius:4px}
  a{color:#0066cc;text-decoration:none;word-break:break-all}
  a:hover{text-decoration:underline}
</style>

<h2>Video → Litterbox (1h Expiry / 1 GB Limit)</h2>
<div class="card">
  <input id="u" placeholder="Video URL">
  <input id="t" placeholder="Access token (if set)" type="password">
  <button onclick="submitJob()">Upload to Queue</button>
</div>

<div id="queueContainer"></div>

<script>
function escapeHtml(str) {
  return (str || '').replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

async function submitJob() {
  const u = document.getElementById('u');
  const t = document.getElementById('t');
  if(!u.value.trim()) return;
  
  const r = await fetch('/api/jobs', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({url: u.value.trim(), token: t.value.trim()})
  });
  
  if(r.ok) {
    u.value = '';
    fetchQueue();
  } else {
    alert('Failed to submit job: HTTP ' + r.status);
  }
}

async function cancelJob(jid) {
  await fetch('/api/jobs/' + jid + '/cancel', {method: 'POST'});
  fetchQueue();
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
      html += '<div class="section-title">Currently Processing</div>';
      active.forEach(j => {
        const sizeStr = j.total_size ? ((j.bytes/1e6).toFixed(1) + '/' + (j.total_size/1e6).toFixed(1) + ' MB') : ((j.bytes/1e6).toFixed(1) + ' MB');
        html += `<div class="card active">
          <div><span class="badge badge-active">${escapeHtml(j.status)}</span></div>
          <div style="margin-top:.4rem"><b>${escapeHtml(j.title || j.source_url)}</b></div>
          ${j.quality ? '<div>Quality: ' + escapeHtml(j.quality) + '</div>' : ''}
          <div>Size: ${sizeStr}</div>
          ${j.log ? '<div style="color:#666;font-size:.85rem;margin-top:.3rem">' + escapeHtml(j.log) + '</div>' : ''}
          <button class="cancel" onclick="cancelJob('${j.id}')">Cancel Job</button>
        </div>`;
      });
    }
    
    if(queued.length > 0) {
      html += '<div class="section-title">Pending Queue (' + queued.length + ')</div>';
      queued.forEach((j, idx) => {
        html += `<div class="card queued">
          <div><span class="badge badge-queued">Queue Position #${idx + 1}</span></div>
          <div style="margin-top:.4rem;word-break:break-all"><b>${escapeHtml(j.source_url)}</b></div>
          <button class="cancel" onclick="cancelJob('${j.id}')">Remove from Queue</button>
        </div>`;
      });
    }
    
    if(finished.length > 0) {
      html += '<div class="section-title">Recent Activity</div>';
      finished.slice(0, 8).forEach(j => {
        const bClass = j.status === 'done' ? 'badge-done' : 'badge-error';
        html += `<div class="card ${j.status}">
          <div><span class="badge ${bClass}">${escapeHtml(j.status)}</span></div>
          <div style="margin-top:.3rem"><b>${escapeHtml(j.title || j.source_url)}</b></div>
          ${j.result_url ? '<div style="margin-top:.4rem"><a href="' + escapeHtml(j.result_url) + '" target="_blank">' + escapeHtml(j.result_url) + '</a></div>' : ''}
          ${j.error ? '<div style="color:#c0392b;font-size:.85rem;margin-top:.3rem">' + escapeHtml(j.error) + '</div>' : ''}
        </div>`;
      });
    }
    
    if(jobs.length === 0) {
      html = '<div style="text-align:center;color:#888;margin:2rem 0">No active or queued jobs.</div>';
    }
    
    document.getElementById('queueContainer').innerHTML = html;
  } catch(e) {}
}

window.addEventListener('DOMContentLoaded', () => {
  fetchQueue();
  setInterval(fetchQueue, 1500);
});
</script>"""
