CSS_STYLES = """
  body { font:15px system-ui,-apple-system,sans-serif;max-width:540px;margin:1.5rem auto;padding:0 1rem;background:#f9f9f9;color:#222 }
  input,button { width:100%;padding:.75rem;margin:.3rem 0;font-size:1rem;box-sizing:border-box;border-radius:6px;border:1px solid #ccc }
  button { background:#0066cc;color:#fff;font-weight:600;border:none;cursor:pointer }
  button:hover { background:#0052a3 }
  
  .input-wrapper { position: relative; width: 100%; }
  .input-wrapper input { padding-right: 42px !important; }
  .input-action-btn {
    position: absolute; right: 8px; top: 50%; transform: translateY(-50%);
    background: transparent !important; border: none !important; padding: 6px !important;
    margin: 0 !important; width: auto !important; cursor: pointer; color: #888;
    display: flex; align-items: center; justify-content: center; border-radius: 4px;
  }
  .input-action-btn:hover { color: #222; }

  details { margin: .6rem 0; border: 1px dashed #bbb; border-radius: 6px; padding: .5rem .8rem; background: #fafafa; }
  summary { font-weight: 600; font-size: .85rem; color: #444; cursor: pointer; }
  .adv-option { margin-top: .4rem; }
  .adv-option input { padding: .5rem; font-size: .85rem; }
  .checkbox-label { display: flex; align-items: center; gap: 8px; font-size: .85rem; color: #333; margin: .4rem 0; cursor: pointer; }
  .checkbox-label input { width: auto !important; margin: 0 !important; }

  .swipe-container { position: relative; overflow: hidden; margin: .8rem 0; border-radius: 8px; }
  .swipe-action-bg {
    position: absolute; top: 0; right: 0; bottom: 0; left: 0; background: #d9534f;
    display: flex; align-items: center; justify-content: flex-end; padding-right: 20px;
    border-radius: 8px; z-index: 1;
  }
  .swipe-action-btn {
    background: transparent !important; border: none !important; color: white !important;
    padding: 10px !important; margin: 0 !important; width: auto !important;
    cursor: pointer; display: flex; align-items: center;
  }

  .card {
    position: relative; z-index: 2; background:#fff; border:1px solid #e0e0e0;
    border-radius:8px; padding:.8rem 1rem; box-shadow:0 1px 3px rgba(0,0,0,0.05);
    transition: transform 0.15s ease-out;
  }
  .card.active { border-left:5px solid #0066cc }
  .card.queued { border-left:5px solid #f0ad4e }
  .card.done { border-left:5px solid #5cb85c }
  .card.error,.card.cancelled { border-left:5px solid #d9534f }
  
  .badge { display:inline-block;padding:.2rem .5rem;font-size:.75rem;font-weight:bold;border-radius:4px;text-transform:uppercase }
  .badge-active { background:#e6f2ff;color:#0066cc }
  .badge-queued { background:#fef5e7;color:#f0ad4e }
  .badge-done { background:#eafaf1;color:#27ae60 }
  .badge-error { background:#fadbd8;color:#c0392b }
  
  .section-header { display: flex; align-items: center; justify-content: space-between; margin: 1.2rem 0 .4rem 0; border-bottom: 1px solid #ddd; padding-bottom: .3rem; }
  .section-title { font-size:1.1rem;font-weight:bold;color:#444 }
  
  .clear-btn {
    width: auto !important; padding: .25rem .6rem !important; font-size: .8rem !important;
    background: transparent !important; color: #888 !important; border: 1px solid #ccc !important;
    border-radius: 4px !important; margin: 0 !important;
  }
  .clear-btn:hover { background: #fee !important; color: #c0392b !important; border-color: #f5c6cb !important; }

  button.cancel { background:#d9534f;color:#fff;padding:.4rem .8rem;font-size:.85rem;width:auto;margin-top:.4rem }
  button.cancel:hover { background:#c9302c }

  .url-row { display: flex; align-items: center; justify-content: space-between; gap: 8px; margin-top: .4rem; }
  .copy-btn {
    width: auto !important; padding: 4px 8px !important; margin: 0 !important;
    background: #f0f0f0 !important; color: #333 !important; border: 1px solid #ccc !important;
    border-radius: 4px !important; display: flex; align-items: center; justify-content: center;
  }
  .copy-btn:hover { background: #e0e0e0 !important; }

  .ytdlp-tag { text-align: center; font-size: 0.78rem; color: #888; margin: 0.8rem 0; user-select: none; }
  a { color:#0066cc;text-decoration:none;word-break:break-all }
  a:hover { text-decoration:underline }
"""
