from fastapi import APIRouter
from fastapi.responses import HTMLResponse
from templates.dashboard import get_dashboard_html

router = APIRouter(tags=["UI"])

@router.get("/", response_class=HTMLResponse)
def home():
    return get_dashboard_html()