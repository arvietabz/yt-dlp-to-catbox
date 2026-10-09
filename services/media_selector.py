import os
from config import MAX_BYTES

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
