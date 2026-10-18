from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api import files, subjects
from app.web import routes as web

app = FastAPI(title="Subject Wiki", version="0.1.0")
app.include_router(subjects.router)
app.include_router(files.router)
app.include_router(web.router)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "web" / "static"), name="static")
