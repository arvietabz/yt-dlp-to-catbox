"""yt-dlp -> ffmpeg (pipe) -> catbox.moe. Nothing is written to disk."""
import io, os, re, subprocess, threading, time, uuid
import requests, yt_dlp
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

MAX_BYTES = int(float(os.getenv("MAX_MB", "190")) * 1_000_000)   # target ceiling
HARD_BYTES = 199_000_000                                          # abort above this
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN", "")
USERHASH = os.getenv("CATBOX_USERHASH", "")
BUFFER_IN_RAM = os.getenv("BUFFER_IN_RAM", "0") == "1"  # fallback if catbox rejects chunked upload
CATBOX = "https://catbox.moe/user/api.php"

app = FastAPI()
JOBS: dict[str, dict] = {}
SEM = threading.Semaphore(int(os.getenv("MAX_CONCURRENT", "1")))


def est_size(f, dur):
    s = f.get("filesize") or f.get("filesize_approx")
    if s:
        return s
    if f.get("tbr") and dur:
        return f["tbr"] * 1000 / 8 * dur
    return None


def pick(info):
    """Best (video+audio) or progressive combo whose total size fits MAX_BYTES."""
    dur = info.get("duration")
    fmts = [f for f in info.get("formats", []) if f.get("url") and f.get("protocol") not in ("mhtml",)]
    vid = [f for f in fmts if f.get("vcodec") not in (None, "none") and f.get("acodec") in (None, "none")]
    aud = [f for f in fmts if f.get("acodec") not in (None, "none") and f.get("vcodec") in (None, "none")]
    prog = [f for f in fmts if f.get("vcodec") not in (None, "none") and f.get("acodec") not in (None, "none")]
    aud.sort(key=lambda f: f.get("abr") or f.get("tbr") or 0, reverse=True)

    cands = []  # (score, [formats], size)
    for p in prog:
        s = est_size(p, dur)
        if s and s <= MAX_BYTES:
            cands.append(((p.get("height") or 0, p.get("tbr") or 0), [p], s))
    for v in vid:
        vs = est_size(v, dur)
        if not vs:
            continue
        for a in aud:  # best audio that still fits
            as_ = est_size(a, dur)
            if as_ and vs + as_ <= MAX_BYTES:
                cands.append(((v.get("height") or 0, v.get("tbr") or 0), [v, a], vs + as_))
                break
    if not cands:
        return None
    cands.sort(key=lambda c: c[0], reverse=True)
    return cands[0]


def build_cmd(chosen):
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin"]
    for f in chosen:
        hdr = "".join(f"{k}: {v}\r\n" for k, v in (f.get("http_headers") or {}).items())
        cmd += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5",
                "-headers", hdr, "-i", f["url"]]
    if len(chosen) == 2:
        cmd += ["-map", "0:v:0", "-map", "1:a:0"]
    acodec = (chosen[-1].get("acodec") or "")
    cmd += ["-c:v", "copy", "-c:a", "copy" if acodec.startswith("mp4a") else "aac"]
    cmd += ["-movflags", "frag_keyframe+empty_moov+default_base_moof", "-f", "mp4", "pipe:1"]
    return cmd


def run_job(jid, url):
    job = JOBS[jid]
    proc = None
    with SEM:
        try:
            job.update(status="analyzing")
            opts = {"quiet": True, "no_warnings": True, "noplaylist": True, "skip_download": True}
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
            choice = pick(info)
            if not choice:
                raise RuntimeError(f"No format fits under {MAX_BYTES // 1_000_000} MB (or sizes unknown).")
            _, chosen, size = choice
            vf = chosen[0]
            job.update(status="streaming",
                       quality=f"{vf.get('height')}p ~{int(size / 1e6)} MB",
                       title=info.get("title"))
            name = re.sub(r"[^A-Za-z0-9_\- ]", "", info.get("title") or "")[:80].strip() or "video"
            boundary = uuid.uuid4().hex
            pre = (f'--{boundary}\r\nContent-Disposition: form-data; name="reqtype"\r\n\r\nfileupload\r\n'
                   + (f'--{boundary}\r\nContent-Disposition: form-data; name="userhash"\r\n\r\n{USERHASH}\r\n' if USERHASH else "")
                   + f'--{boundary}\r\nContent-Disposition: form-data; name="fileToUpload"; filename="{name}.mp4"\r\n'
                     f'Content-Type: video/mp4\r\n\r\n').encode()
            post = f"\r\n--{boundary}--\r\n".encode()
            headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}

            proc = subprocess.Popen(build_cmd(chosen), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            sent = 0

            def chunks():
                nonlocal sent
                while True:
                    c = proc.stdout.read(1 << 20)
                    if not c:
                        return
                    sent += len(c)
                    job["bytes"] = sent
                    if sent > HARD_BYTES:
                        proc.kill()
                        raise RuntimeError("Output exceeded the 200 MB limit; aborted.")
                    yield c

            if BUFFER_IN_RAM:  # exact Content-Length, uses RAM instead of disk
                buf = io.BytesIO()
                buf.write(pre)
                for c in chunks():
                    buf.write(c)
                buf.write(post)
                if proc.wait() != 0:
                    raise RuntimeError("ffmpeg failed: " + proc.stderr.read().decode()[-300:])
                buf.seek(0)
                job["status"] = "uploading"
                r = requests.post(CATBOX, data=buf, headers=headers, timeout=900)
            else:  # true streaming, chunked transfer encoding
                def body():
                    yield pre
                    yield from chunks()
                    yield post
                r = requests.post(CATBOX, data=body(), headers=headers, timeout=900)
                if proc.wait() != 0:
                    raise RuntimeError("ffmpeg failed mid-stream: " + proc.stderr.read().decode()[-300:])

            out = r.text.strip()
            if not out.startswith("http"):
                raise RuntimeError(f"Catbox said: {out[:200]}")
            job.update(status="done", url=out)
        except Exception as e:
            if proc and proc.poll() is None:
                proc.kill()
            job.update(status="error", error=str(e)[:400])


class Req(BaseModel):
    url: str
    token: str = ""


@app.post("/api/jobs")
def create(req: Req):
    if ACCESS_TOKEN and req.token != ACCESS_TOKEN:
        raise HTTPException(401, "bad token")
    if not re.match(r"^https?://", req.url):
        raise HTTPException(400, "invalid url")
    for k in list(JOBS)[:-30]:  # keep memory small
        JOBS.pop(k, None)
    jid = uuid.uuid4().hex[:10]
    JOBS[jid] = {"status": "queued", "bytes": 0, "t": time.time()}
    threading.Thread(target=run_job, args=(jid, req.url), daemon=True).start()
    return {"id": jid}


@app.get("/api/jobs/{jid}")
def status(jid: str):
    if jid not in JOBS:
        raise HTTPException(404)
    return JOBS[jid]


@app.get("/", response_class=HTMLResponse)
def home():
    return """<!doctype html><meta name=viewport content="width=device-width,initial-scale=1">
<title>yt → catbox</title>
<style>body{font:16px system-ui;max-width:520px;margin:2rem auto;padding:0 1rem}
input,button{width:100%;padding:.8rem;margin:.3rem 0;font-size:1rem;box-sizing:border-box}
pre{white-space:pre-wrap;word-break:break-all}</style>
<h2>Video → Catbox</h2>
<input id=u placeholder="Video URL"><input id=t placeholder="Access token (if set)" type=password>
<button onclick=go()>Upload</button><pre id=o></pre>
<script>
async function go(){const o=document.getElementById('o');o.textContent='Starting…';
const r=await fetch('/api/jobs',{method:'POST',headers:{'Content-Type':'application/json'},
body:JSON.stringify({url:u.value,token:t.value})});
if(!r.ok){o.textContent='Error '+r.status;return}
const {id}=await r.json();
const i=setInterval(async()=>{const j=await (await fetch('/api/jobs/'+id)).json();
o.textContent=j.status+(j.quality?'\\n'+j.quality:'')+(j.bytes?'\\n'+(j.bytes/1e6).toFixed(1)+' MB':'')
+(j.error?'\\n'+j.error:'')+(j.url?'\\n'+j.url:'');
if(j.status=='done'||j.status=='error')clearInterval(i)},1500)}
</script>"""
