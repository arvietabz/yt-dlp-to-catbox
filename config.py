import ipaddress
import os
import urllib.parse

MAX_BYTES = int(float(os.getenv("MAX_MB", "1000")) * 1_000_000)
HARD_BYTES = 1_050_000_000
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN", "").strip()
LITTERBOX_URL = "https://litterbox.catbox.moe/resources/internals/api.php"

YT_COOKIES = os.getenv("YT_COOKIES", "").strip()
COOKIE_PATH = "/tmp/cookies.txt"

if YT_COOKIES:
    with open(COOKIE_PATH, "w") as _f:
        _f.write(YT_COOKIES)

RAW_PROXY = os.getenv("PROXY", "").strip()

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

if IS_TAILSCALE_PROXY:
    if REMOTE_PROXY_USER or REMOTE_PROXY_PASS:
        PROXY = f"http://{REMOTE_PROXY_USER}:{REMOTE_PROXY_PASS}@127.0.0.1:{LOCAL_BRIDGE_PORT}"
    else:
        PROXY = f"http://127.0.0.1:{LOCAL_BRIDGE_PORT}"
else:
    PROXY = RAW_PROXY