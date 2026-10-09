import state
from templates.styles import CSS_STYLES
from templates.scripts import JS_SCRIPTS

def get_dashboard_html() -> str:
    return f"""<!doctype html><meta name=viewport content="width=device-width,initial-scale=1">
<title>yt → litterbox</title>
<style>
{CSS_STYLES}
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

      <!-- Custom Cookies Controls -->
      <label class="checkbox-label">
        <input type="checkbox" id="useCookies" onchange="handleCookiesCheckboxChange()">
        🍪 Use Custom cookies.txt
        <span id="cookiesStatus" style="color:#27ae60;font-weight:600;display:none;margin-left:4px">(cookies.txt imported)</span>
      </label>
      <div id="cookiesUploadWrapper" style="display:none;margin:.4rem 0 .4rem 24px;">
        <input type="file" id="cookiesFileInput" accept=".txt" onchange="handleCookieFileUpload(event)" style="font-size:.85rem;padding:.4rem;">
      </div>
    </div>
  </details>

  <input id="t" placeholder="Access token (if set)" type="password">
  <button onclick="submitJob()">Upload to Queue</button>
</div>

<div class="ytdlp-tag" id="ytdlpTag">yt-dlp v{state.CURRENT_YTDLP_VERSION}</div>

<div id="queueContainer"></div>

<div id="activityContainer"></div>

<script>
{JS_SCRIPTS}
</script>"""
