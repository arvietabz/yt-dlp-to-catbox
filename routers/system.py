import requests
import yt_dlp
from fastapi import APIRouter, HTTPException
from config import COOKIE_PATH, IS_TAILSCALE_PROXY, PROXY, RAW_PROXY, YT_COOKIES
import state
from services.updater import normalize_ver, perform_ytdlp_update

router = APIRouter(prefix="/api", tags=["System"])

@router.post("/update-ytdlp")
async def trigger_ytdlp_update():
    try:
        log_output = await perform_ytdlp_update()
        return {
            "status": "success",
            "version": state.CURRENT_YTDLP_VERSION,
            "log": log_output
        }
    except Exception as e:
        raise HTTPException(500, f"Update failed: {str(e)}")

@router.get("/version")
def get_version():
    latest_version = state.CURRENT_YTDLP_VERSION
    is_latest = True
    try:
        r = requests.get("https://pypi.org/pypi/yt-dlp/json", timeout=3)
        if r.status_code == 200:
            latest_version = r.json().get("info", {}).get("version", state.CURRENT_YTDLP_VERSION)
            is_latest = (normalize_ver(state.CURRENT_YTDLP_VERSION) == normalize_ver(latest_version))
    except Exception:
        pass

    return {
        "current_version": state.CURRENT_YTDLP_VERSION,
        "latest_version": latest_version,
        "is_latest": is_latest
    }

@router.get("/test-proxy")
def test_proxy():
    if not PROXY:
        return {"status": "error", "message": "PROXY environment variable is empty in Render."}
    
    proxies = {"http": PROXY, "https": PROXY}
    
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

@router.get("/debug-url-test")
@router.get("/debug-url")
def debug_url(url: str, use_proxy: bool = True):
    job_proxy = PROXY if use_proxy else ""
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "extractor_args": {
            "youtube": {
                "player_client": ["android", "web", "mweb"]
            }
        }
    }
    if job_proxy:
        opts["proxy"] = job_proxy
    if YT_COOKIES:
        opts["cookiefile"] = COOKIE_PATH

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            
        formats = info.get("formats", [])
        parsed_formats = [{
            "format_id": f.get("format_id"),
            "ext": f.get("ext"),
            "height": f.get("height"),
            "vcodec": f.get("vcodec"),
            "acodec": f.get("acodec"),
            "filesize_mb": round((f.get("filesize") or f.get("filesize_approx") or 0) / 1e6, 2),
            "tbr": f.get("tbr"),
            "protocol": f.get("protocol")
        } for f in formats]

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
