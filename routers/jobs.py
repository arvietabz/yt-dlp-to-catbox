import re
import time
import uuid
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from config import ACCESS_TOKEN
import state

router = APIRouter(prefix="/api/jobs", tags=["Jobs"])

class Req(BaseModel):
    url: str = ""
    token: str = ""
    referer: str = ""
    user_agent: str = ""
    custom_title: str = ""
    direct_mode: bool = False
    force_generic: bool = False
    use_proxy: bool = False

@router.get("")
def list_all_jobs():
    items = [{"id": k, **v} for k, v in state.JOBS.items()]
    items.sort(key=lambda x: x["t"], reverse=True)
    return items

@router.post("")
async def create(req: Req):
    if ACCESS_TOKEN and req.token != ACCESS_TOKEN:
        raise HTTPException(401, "bad token")
    if not re.match(r"^https?://", req.url):
        raise HTTPException(400, "invalid url")
    
    for k in list(state.JOBS)[:-30]:
        state.JOBS.pop(k, None)
        
    jid = uuid.uuid4().hex[:10]
    state.JOBS[jid] = {
        "source_url": req.url,
        "status": "queued",
        "bytes": 0,
        "log": "Queued for processing...",
        "t": time.time(),
        "cancelled": False,
        "referer": req.referer.strip(),
        "user_agent": req.user_agent.strip(),
        "custom_title": req.custom_title.strip(),
        "direct_mode": req.direct_mode,
        "force_generic": req.force_generic,
        "use_proxy": req.use_proxy
    }
    await state.JOB_QUEUE.put(jid)
    return {"id": jid}

@router.post("/clear-history")
def clear_history():
    to_remove = [k for k, v in state.JOBS.items() if v["status"] in ("done", "error", "cancelled")]
    for k in to_remove:
        state.JOBS.pop(k, None)
    return {"status": "cleared", "count": len(to_remove)}

@router.get("/{jid}")
def get_job(jid: str):
    if jid not in state.JOBS:
        raise HTTPException(404, "Job not found")
    return state.JOBS[jid]

@router.post("/{jid}/cancel")
def cancel_job(jid: str):
    if jid not in state.JOBS:
        raise HTTPException(404, "Job not found")
    job = state.JOBS[jid]
    if job["status"] in ("done", "error", "cancelled"):
        return {"status": job["status"], "message": "Job already finished"}
    job["cancelled"] = True
    job["status"] = "cancelled"
    job["log"] = "Job cancelled by user."
    return {"status": "cancelled"}

@router.delete("/{jid}")
def delete_job(jid: str):
    if jid in state.JOBS:
        state.JOBS.pop(jid, None)
        return {"status": "deleted"}
    raise HTTPException(404, "Job not found")
