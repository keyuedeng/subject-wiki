from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api import ask, files, subjects
from app.jobs.worker import get_queue
from app.web import ask as web_ask
from app.web import routes as web


@asynccontextmanager
async def lifespan(app: FastAPI):
    queue = get_queue()
    queue.resume()  # parses interrupted by a restart pick up where the cache left off
    yield
    queue.shutdown()


app = FastAPI(title="Subject Wiki", version="0.4.0", lifespan=lifespan)
app.include_router(subjects.router)
app.include_router(files.router)
app.include_router(ask.router)
app.include_router(web.router)
app.include_router(web_ask.router)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "web" / "static"), name="static")
