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
from .ha_correlator import correlate_step_event
from .inference import match_event_to_device
from .mqtt_publisher import publisher
from .shelly_client import run_websocket_loop
from .state import state

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("shelly3em")

templates = Jinja2Templates(directory="app/web/templates")


async def on_sample(sample: dict) -> None:
    state.last_sample = sample

    if not state.first_sample_logged:
        log.info(
            "First sample from Shelly: total=%.1fW A=%.1fW B=%.1fW C=%.1fW",
            sample.get("total_power") or 0.0,
            sample.get("a_power") or 0.0,
            sample.get("b_power") or 0.0,
            sample.get("c_power") or 0.0,
        )
        state.first_sample_logged = True

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

            # 1. HA correlation: if this step matches a recent state change from
            #    a tracked HA entity, attribute it directly (may auto-promote).
            try:
                correlate_step_event(event_id, event)
            except Exception:
                log.exception("HA correlation failed")

            # 2. Cluster-based matcher: handles events that didn't correlate,
            #    plus pre-promotion events on entities that haven't hit the
            #    threshold yet.
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


# Single source of truth for the add-on version — also exposed via /api/info
# and stamped into the dashboard's static asset URLs as a cache-buster.
APP_VERSION = "0.4.1"


# Wrap StaticFiles with no-cache headers so even if the browser still asks
# for /static/app.js, it gets the fresh bytes — defence-in-depth alongside
# the query-string cache busting below.
class NoCacheStaticFiles(StaticFiles):
    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-store, max-age=0, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        return response


app.mount("/static", NoCacheStaticFiles(directory="app/web/static"), name="static")


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
            "version": APP_VERSION,
        },
    )
