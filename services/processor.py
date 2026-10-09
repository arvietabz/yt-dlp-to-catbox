import asyncio
import os
import re
import subprocess
import time
import yt_dlp
from config import COOKIE_PATH, HARD_BYTES, LITTERBOX_URL, MAX_BYTES, PROXY, YT_COOKIES
import state

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
    
    vid, aud, prog = [], [], []

    for f in fmts:
        v, a = f.get("vcodec"), f.get("acodec")
        if v == "none" and a not in (None, "none"):
            aud.append(f)
        elif a == "none" and v not in (None, "none"):
            vid.append(f)
        elif v != "none" and a != "none":
            prog.append(f)
        elif (f.get("height") or 0) > 0 or f.get("ext") in ("mp4", "webm", "m3u8", "mov", "flv"):
            prog.append(f)

    aud.sort(key=lambda f: f.get("abr") or f.get("tbr") or 0, reverse=True)

    cands = []
    for p in prog:
        s = est_size(p, dur)
        if 0 <= s <= MAX_BYTES:
            cands.append((p.get("height") or 0, p.get("tbr") or 0, [p], s if s > 0 else 50_000_000))

    for v in vid:
        vs = est_size(v, dur)
        for a in aud:
            as_ = est_size(a, dur)
            tot = vs + as_
            if 0 <= tot <= MAX_BYTES:
                cands.append((v.get("height") or 0, v.get("tbr") or 0, [v, a], tot if tot > 0 else 50_000_000))
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
    dur = info.get("duration") or 180
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
    # Added -progress pipe:2 to stream real-time progress metrics to stderr
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-progress", "pipe:2", "-y", "-nostdin"]
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
    job = state.JOBS[jid]
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

        choice, enc, info = None, None, {}

        if job.get("direct_mode"):
            job["status"] = "direct streaming"
            job["log"] = "Bypassing yt-dlp, passing stream directly to FFmpeg..."
            chosen = [{"url": source_url, "http_headers": custom_headers, "height": 0, "acodec": "aac", "vcodec": "h264"}]
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
                "extractor_args": {
                    "youtube": {
                        "player_client": ["android", "ios"]
                    }
                }
            }
            if custom_headers:
                opts["http_headers"] = custom_headers
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
        total_duration = info.get("duration") or 0
        
        job.update(
            status="downloading & processing",
            quality=f"{vf.get('height') or 'stream'}p",
            title=title_text,
            log=f"Processing video streams with ffmpeg {'(via Proxy)' if job_proxy else ''}..."
        )

        cmd = build_ffmpeg_cmd(chosen, out_path, enc, extra_headers=custom_headers, proxy=job_proxy)
        proc = subprocess.Popen(cmd, stderr=subprocess.PIPE, text=True, bufsize=1)

        stderr_logs = []
        while True:
            line = proc.stderr.readline() if proc.stderr else ""
            if not line:
                if proc.poll() is not None:
                    break
                time.sleep(0.01)
                continue

            stderr_logs.append(line)
            if len(stderr_logs) > 50:
                stderr_logs.pop(0)

            if job.get("cancelled"):
                proc.kill()
                raise RuntimeError("Job cancelled by user.")

            match = re.search(r"(?:out_time|time)=(\d+):(\d+):(\d+\.\d+|\d+)", line)
            if match and total_duration > 0:
                h, m, s = float(match.group(1)), float(match.group(2)), float(match.group(3))
                curr_sec = h * 3600 + m * 60 + s
                pct = min(99.9, (curr_sec / total_duration) * 100)
                job["download_pct"] = round(pct, 1)

            if os.path.exists(out_path):
                curr_size = os.path.getsize(out_path)
                job["bytes"] = curr_size
                
                dl_pct = job.get("download_pct")
                if dl_pct is not None:
                    job["log"] = f"Processing video streams: {dl_pct:.1f}% ({curr_size / 1e6:.1f} MB)"
                else:
                    job["log"] = f"Processing video streams with ffmpeg ({curr_size / 1e6:.1f} MB)..."

                if curr_size > HARD_BYTES:
                    proc.kill()
                    raise RuntimeError("File size exceeded 1 GB ceiling during processing; aborted.")

        if proc.returncode != 0:
            err_msg = "".join(stderr_logs)[-400:]
            raise RuntimeError(f"FFmpeg failed (exit code {proc.returncode}): {err_msg}")

        if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
            raise RuntimeError("FFmpeg completed but produced an empty file.")

        if job.get("cancelled"):
            raise RuntimeError("Job cancelled by user.")

        final_size = os.path.getsize(out_path)
        job.update(
            status="uploading to litterbox (1h expiry)",
            total_size=final_size,
            bytes=0,
            log=f"Uploading to Litterbox: 0.0% (0.0 / {final_size / 1e6:.1f} MB)"
        )

        clean_title = re.sub(r"[^A-Za-z0-9_\- ]", "", title_text)[:60].strip() or "video"
        ua = job.get("user_agent") or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

        curl_cmd = [
            "curl", "-#", "-S",
            "-A", ua,
            "-F", "reqtype=fileupload",
            "-F", "time=1h",
            "-F", f"fileToUpload=@{out_path};filename={clean_title}.mp4",
            LITTERBOX_URL
        ]

        upload_proc = subprocess.Popen(
            curl_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1
        )

        buffer = ""
        while upload_proc.poll() is None:
            if job.get("cancelled"):
                upload_proc.kill()
                raise RuntimeError("Job cancelled by user.")

            char = upload_proc.stderr.read(1)
            if not char:
                time.sleep(0.05)
                continue

            if char in ("\r", "\n"):
                line = buffer.strip()
                if line:
                    match = re.search(r"(\d+(?:\.\d+)?)%", line)
                    if match:
                        pct = float(match.group(1))
                        uploaded_bytes = int((pct / 100.0) * final_size)
                        job["bytes"] = uploaded_bytes
                        job["log"] = f"Uploading to Litterbox: {pct:.1f}% ({uploaded_bytes / 1e6:.1f} / {final_size / 1e6:.1f} MB)"
                buffer = ""
            else:
                buffer += char

        if job.get("cancelled"):
            raise RuntimeError("Job cancelled by user.")

        stdout_data, stderr_data = upload_proc.communicate()

        if upload_proc.returncode != 0:
            raise RuntimeError(f"cURL upload failed (exit {upload_proc.returncode}): {stderr_data[:200]}")

        out_url = stdout_data.strip()
        if not out_url.startswith("http"):
            raise RuntimeError(f"Litterbox response error: '{out_url[:200]}'")

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
        jid = await state.JOB_QUEUE.get()
        job = state.JOBS.get(jid)
        if job and not job.get("cancelled") and job["status"] == "queued":
            await asyncio.to_thread(process_job_sync, jid, job["source_url"])
        state.JOB_QUEUE.task_done()
