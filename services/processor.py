import asyncio
import os
import re
import subprocess
import time
import yt_dlp
from config import COOKIE_PATH, HARD_BYTES, LITTERBOX_URL, MAX_BYTES, PROXY, YT_COOKIES
import state
from services.media_selector import pick, plan_transcode
from services.ffmpeg_builder import build_ffmpeg_cmd

def process_job_sync(jid, source_url):
    job = state.JOBS[jid]
    out_path = f"/tmp/{jid}.mp4"
    job_cookie_path = None
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

            # Always set YouTube mobile client fallback regardless of custom cookies
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

            # Parse custom extractor args (e.g. generic:impersonate)
            if job.get("custom_args"):
                try:
                    from yt_dlp.utils import parse_map_param
                    raw_args = [a for a in re.split(r'[\s;]+', job["custom_args"]) if a]
                    parsed_args = parse_map_param(raw_args)

                    for ext_key, ext_val in parsed_args.items():
                        if ext_key in opts["extractor_args"]:
                            opts["extractor_args"][ext_key].update(ext_val)
                        else:
                            opts["extractor_args"][ext_key] = ext_val
                except Exception as e:
                    job["log"] = f"Warning: Failed to parse custom_args ({e})"

            if custom_headers:
                opts["http_headers"] = custom_headers
            if job_proxy:
                opts["proxy"] = job_proxy

            if job.get("cookies"):
                job_cookie_path = f"/tmp/{jid}_cookies.txt"
                with open(job_cookie_path, "w", encoding="utf-8") as _cf:
                    _cf.write(job["cookies"])
                opts["cookiefile"] = job_cookie_path
            elif YT_COOKIES:
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
            total_size=size if (size and size > 0 and size < HARD_BYTES) else None,
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
                if dl_pct and dl_pct > 0.5:
                    job["total_size"] = int(curr_size / (dl_pct / 100.0))

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
        if job_cookie_path and os.path.exists(job_cookie_path):
            try:
                os.remove(job_cookie_path)
            except Exception:
                pass

async def queue_worker():
    while True:
        jid = await state.JOB_QUEUE.get()
        job = state.JOBS.get(jid)
        if job and not job.get("cancelled") and job["status"] == "queued":
            await asyncio.to_thread(process_job_sync, jid, job["source_url"])
        state.JOB_QUEUE.task_done()
