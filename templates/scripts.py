JS_SCRIPTS = """
function escapeHtml(str) {
  return (str || '').replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

const openCards = new Set();
const copiedJobs = new Set();
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

function updateCustomArgsUI() {
  const useCustomArgs = document.getElementById('useCustomArgs');
  const customArgsWrapper = document.getElementById('customArgsWrapper');
  const customArgsInput = document.getElementById('customArgsInput');
  const savedVal = localStorage.getItem('custom_args_val') || '';

  if (customArgsInput && savedVal) {
    customArgsInput.value = savedVal;
  }

  if (useCustomArgs && useCustomArgs.checked) {
    customArgsWrapper.style.display = 'block';
  } else if (customArgsWrapper) {
    customArgsWrapper.style.display = 'none';
  }
}

function handleCustomArgsCheckboxChange() {
  const useCustomArgs = document.getElementById('useCustomArgs');
  if (useCustomArgs.checked) {
    localStorage.setItem('use_custom_args', 'true');
  } else {
    localStorage.setItem('use_custom_args', 'false');
  }
  updateCustomArgsUI();
}

function handleCustomArgsInput(e) {
  localStorage.setItem('custom_args_val', e.target.value);
}

function updateCookiesUI() {
  const useCookies = document.getElementById('useCookies');
  const cookiesStatus = document.getElementById('cookiesStatus');
  const cookiesUploadWrapper = document.getElementById('cookiesUploadWrapper');
  const savedCookies = localStorage.getItem('custom_cookies');

  if (useCookies.checked) {
    if (savedCookies) {
      cookiesStatus.style.display = 'inline';
      cookiesUploadWrapper.style.display = 'none';
    } else {
      cookiesStatus.style.display = 'none';
      cookiesUploadWrapper.style.display = 'block';
    }
  } else {
    cookiesStatus.style.display = 'none';
    cookiesUploadWrapper.style.display = 'none';
  }
}

function handleCookiesCheckboxChange() {
  const useCookies = document.getElementById('useCookies');
  if (useCookies.checked) {
    localStorage.setItem('use_cookies', 'true');
  } else {
    localStorage.removeItem('use_cookies');
    localStorage.removeItem('custom_cookies');
    const input = document.getElementById('cookiesFileInput');
    if (input) input.value = '';
  }
  updateCookiesUI();
}

function handleCookieFileUpload(e) {
  const file = e.target.files[0];
  if (!file) return;
  const reader = new FileReader();
  reader.onload = function(evt) {
    const text = evt.target.result;
    if (text) {
      localStorage.setItem('custom_cookies', text);
      localStorage.setItem('use_cookies', 'true');
      updateCookiesUI();
    }
  };
  reader.readAsText(file);
}

async function triggerUpdate() {
  const el = document.getElementById('ytdlpTag');
  if(el) el.innerHTML = 'yt-dlp <span style="color:#0066cc">Updating...</span>';
  try {
    const res = await fetch('/api/update-ytdlp', {method: 'POST'});
    if(res.ok) {
      fetchVersion();
    }
  } catch(e) {}
}

async function fetchVersion() {
  try {
    const res = await fetch('/api/version');
    if(res.ok) {
      const data = await res.json();
      const el = document.getElementById('ytdlpTag');
      if(el && data.current_version) {
        if(data.is_latest) {
          el.innerHTML = '<span onclick="triggerUpdate()" style="cursor:pointer" title="Click to force update">yt-dlp v' + escapeHtml(data.current_version) + ' <span style="color:#27ae60;font-weight:600">(Up to date)</span></span>';
        } else {
          el.innerHTML = '<span onclick="triggerUpdate()" style="cursor:pointer" title="Click to update now">yt-dlp v' + escapeHtml(data.current_version) + ' <span style="color:#e67e22;font-weight:600">(Update available: v' + escapeHtml(data.latest_version) + ')</span></span>';
        }
      }
    }
  } catch(e) {}
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

  const useCustomArgs = document.getElementById('useCustomArgs');
  const isCustomArgsEnabled = localStorage.getItem('use_custom_args') === 'true';
  if (useCustomArgs) {
    useCustomArgs.checked = isCustomArgsEnabled;
    updateCustomArgsUI();
  }

  const useCookies = document.getElementById('useCookies');
  const isCookiesEnabled = localStorage.getItem('use_cookies') === 'true';
  if (isCookiesEnabled && localStorage.getItem('custom_cookies')) {
    useCookies.checked = true;
  } else {
    useCookies.checked = false;
    localStorage.removeItem('use_cookies');
    localStorage.removeItem('custom_cookies');
  }
  updateCookiesUI();

  fetchQueue();
  fetchVersion();
  setInterval(fetchQueue, 1500);
  setInterval(fetchVersion, 60000);
});

async function submitJob() {
  const u = document.getElementById('u');
  const t = document.getElementById('t');
  const ref = document.getElementById('ref');
  const ua = document.getElementById('ua');
  const ctitle = document.getElementById('ctitle');
  const directMode = document.getElementById('directMode');
  const forceGeneric = document.getElementById('forceGeneric');
  const useProxy = document.getElementById('useProxy');
  const useCookies = document.getElementById('useCookies');
  const useCustomArgs = document.getElementById('useCustomArgs');
  const customArgsInput = document.getElementById('customArgsInput');

  if(!u.value.trim()) return;
  
  if(t.value.trim()) {
    localStorage.setItem('access_token', t.value.trim());
  }

  const cookiesText = (useCookies && useCookies.checked) ? (localStorage.getItem('custom_cookies') || '') : '';
  const customArgsText = (useCustomArgs && useCustomArgs.checked && customArgsInput) ? customArgsInput.value.trim() : '';

  const payload = {
    url: u.value.trim(),
    token: t.value.trim(),
    referer: ref.value.trim(),
    user_agent: ua.value.trim(),
    custom_title: ctitle.value.trim(),
    direct_mode: directMode.checked,
    force_generic: forceGeneric.checked,
    use_proxy: useProxy.checked,
    cookies: cookiesText,
    custom_args: customArgsText
  };

  const r = await fetch('/api/jobs', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload)
  });
  
  if(r.ok) {
    u.value = '';
    ref.value = '';
    ua.value = '';
    ctitle.value = '';
    directMode.checked = false;
    forceGeneric.checked = false;
    useProxy.checked = false;
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

function copyToClipboard(text, jid) {
  navigator.clipboard.writeText(text).then(() => {
    copiedJobs.add(jid);
    fetchQueue();
    setTimeout(() => {
      copiedJobs.delete(jid);
      fetchQueue();
    }, 3000);
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
    
    let queueHtml = '';
    let activityHtml = '';
    
    if(active.length > 0) {
      queueHtml += '<div class="section-header"><div class="section-title">Currently Processing</div></div>';
      active.forEach(j => {
        const isOpen = openCards.has(j.id);
        const transformStyle = isOpen ? 'transform: translateX(-70px);' : '';
        const sizeStr = j.total_size 
          ? ((j.bytes/1e6).toFixed(1) + ' / ' + (j.total_size/1e6).toFixed(1) + ' MB') 
          : ((j.bytes/1e6).toFixed(1) + ' MB');
        const pctBadge = (j.download_pct !== undefined && j.download_pct !== null) 
          ? `<span class="badge badge-active" style="margin-left:4px">${j.download_pct}%</span>` 
          : '';

        queueHtml += `<div class="swipe-container">
          <div class="swipe-action-bg">
            <button class="swipe-action-btn" onclick="deleteCard('${j.id}')">
              <svg width="22" height="22" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M3 6h18M19 6v14a2 2 0 01-2 2H7a2 2 0 01-2-2V6m3 0V4a2 2 0 012-2h4a2 2 0 012 2v2M10 11v6M14 11v6"/></svg>
            </button>
          </div>
          <div class="card active" id="card-el-${j.id}" style="${transformStyle}" ontouchstart="handleTouchStart(event, '${j.id}')" ontouchmove="handleTouchMove(event, '${j.id}')" ontouchend="handleTouchEnd(event, '${j.id}')">
            <div>
              <span class="badge badge-active">${escapeHtml(j.status)}</span>
              ${pctBadge}
              ${j.use_proxy ? '<span class="badge badge-queued" style="margin-left:4px">PROXY ON</span>' : ''}
            </div>
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
      queueHtml += '<div class="section-header"><div class="section-title">Pending Queue (' + queued.length + ')</div></div>';
      queued.forEach((j, idx) => {
        const isOpen = openCards.has(j.id);
        const transformStyle = isOpen ? 'transform: translateX(-70px);' : '';
        
        queueHtml += `<div class="swipe-container">
          <div class="swipe-action-bg">
            <button class="swipe-action-btn" onclick="deleteCard('${j.id}')">
              <svg width="22" height="22" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M3 6h18M19 6v14a2 2 0 01-2 2H7a2 2 0 01-2-2V6m3 0V4a2 2 0 012-2h4a2 2 0 012 2v2M10 11v6M14 11v6"/></svg>
            </button>
          </div>
          <div class="card queued" id="card-el-${j.id}" style="${transformStyle}" ontouchstart="handleTouchStart(event, '${j.id}')" ontouchmove="handleTouchMove(event, '${j.id}')" ontouchend="handleTouchEnd(event, '${j.id}')">
            <div>
              <span class="badge badge-queued">Queue Position #${idx + 1}</span>
              ${j.use_proxy ? '<span class="badge badge-queued" style="margin-left:4px">PROXY ON</span>' : ''}
            </div>
            <div style="margin-top:.4rem;word-break:break-all"><b>${escapeHtml(j.source_url)}</b></div>
            <button class="cancel" onclick="cancelJob('${j.id}')">Remove from Queue</button>
          </div>
        </div>`;
      });
    }
    
    if(finished.length > 0) {
      activityHtml += `<div class="section-header">
        <div class="section-title">Recent Activity</div>
        <button class="clear-btn" onclick="clearAllHistory()">Clear All</button>
      </div>`;
      finished.slice(0, 10).forEach(j => {
        const isOpen = openCards.has(j.id);
        const transformStyle = isOpen ? 'transform: translateX(-70px);' : '';
        const bClass = j.status === 'done' ? 'badge-done' : 'badge-error';
        const isCopied = copiedJobs.has(j.id);
        const copyBtnContent = isCopied
          ? '<span style="font-size:11px;color:#27ae60">✓ Copied</span>'
          : '<svg width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1"/></svg>';

        const copyBtn = j.result_url ? `<button class="copy-btn" onclick="copyToClipboard('${escapeHtml(j.result_url)}', '${j.id}')" title="Copy Link">${copyBtnContent}</button>` : '';

        activityHtml += `<div class="swipe-container">
          <div class="swipe-action-bg">
            <button class="swipe-action-btn" onclick="deleteCard('${j.id}')">
              <svg width="22" height="22" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M3 6h18M19 6v14a2 2 0 01-2 2H7a2 2 0 01-2-2V6m3 0V4a2 2 0 012-2h4a2 2 0 012 2v2M10 11v6M14 11v6"/></svg>
            </button>
          </div>
          <div class="card ${j.status}" id="card-el-${j.id}" style="${transformStyle}" ontouchstart="handleTouchStart(event, '${j.id}')" ontouchmove="handleTouchMove(event, '${j.id}')" ontouchend="handleTouchEnd(event, '${j.id}')">
            <div>
              <span class="badge ${bClass}">${escapeHtml(j.status)}</span>
              ${j.use_proxy ? '<span class="badge badge-queued" style="margin-left:4px">PROXY ON</span>' : ''}
            </div>
            <div style="margin-top:.3rem"><b>${escapeHtml(j.title || j.source_url)}</b></div>
            ${j.result_url ? '<div class="url-row"><a href="' + escapeHtml(j.result_url) + '" target="_blank">' + escapeHtml(j.result_url) + '</a>' + copyBtn + '</div>' : ''}
            ${j.error ? '<div style="color:#c0392b;font-size:.85rem;margin-top:.3rem">' + escapeHtml(j.error) + '</div>' : ''}
          </div>
        </div>`;
      });
    }
    
    if(jobs.length === 0) {
      activityHtml = '<div style="text-align:center;color:#888;margin:2rem 0">No active or queued jobs.</div>';
    }
    
    document.getElementById('queueContainer').innerHTML = queueHtml;
    document.getElementById('activityContainer').innerHTML = activityHtml;
  } catch(e) {}
}
"""
