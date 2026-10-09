import os

def build_ffmpeg_cmd(chosen, out_path, enc=None, extra_headers=None, proxy=None):
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
