import asyncio
import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from .api import router as api_router
from .clusterer import cluster_loop
from .config import settings
from .db import init_db, insert_event, insert_sample, prune_old_samples
from .event_detector import StepEventDetector
from .inference import match_event_to_device
from .mqtt_publisher import publisher
from .shelly_client import run_websocket_loop

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("shelly3em")

templates = Jinja2Templates(directory="app/web/templates")


class AppState:
    def __init__(self) -> None:
        self.detector = StepEventDetector()
        self.last_sample: dict = {}
        self.last_persist_ts: float = 0.0
        self.last_prune_ts: float = 0.0


state = AppState()


async def on_sample(sample: dict) -> None:
    state.last_sample = sample

    # Downsample raw samples to disk
    if sample["ts"] - state.last_persist_ts >= settings.sample_downsample_s:
        try:
            insert_sample(sample)
            state.last_persist_ts = sample["ts"]
        except Exception:
            log.exception("Failed to persist sample")

    # Periodic prune
    if sample["ts"] - state.last_prune_ts > 3600:
        try:
            n = prune_old_samples()
            if n:
                log.info("Pruned %d old samples", n)
            state.last_prune_ts = sample["ts"]
        except Exception:
            log.exception("Prune failed")

    # MQTT live state
    publisher.publish_state(sample)

    # Event detection
    event = state.detector.push(sample)
    if event:
        try:
            event_id = insert_event(event)
            device_id = match_event_to_device(event_id, event)
            if device_id is not None:
                publisher.publish_device_state(device_id, event["direction"])
        except Exception:
            log.exception("Event handling failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    publisher.start()
    stop = asyncio.Event()
    ws_task = asyncio.create_task(run_websocket_loop(on_sample, stop))
    cluster_task = asyncio.create_task(cluster_loop(stop))
    log.info("Startup complete (Shelly=%s, MQTT=%s)",
             settings.shelly_host,
             "on" if settings.mqtt_enabled else "off")
    try:
        yield
    finally:
        stop.set()
        ws_task.cancel()
        cluster_task.cancel()
        await asyncio.gather(ws_task, cluster_task, return_exceptions=True)
        publisher.stop()


app = FastAPI(title="Shelly 3EM Smart Monitor", lifespan=lifespan)
app.include_router(api_router)
app.mount("/static", StaticFiles(directory="app/web/static"), name="static")


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "channel_a": settings.channel_a_label,
            "channel_b": settings.channel_b_label,
            "channel_c": settings.channel_c_label,
            "shelly_host": settings.shelly_host,
            "mqtt_enabled": settings.mqtt_enabled,
        },
    )
