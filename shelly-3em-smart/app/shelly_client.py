import asyncio
import json
import logging
import time
from typing import Awaitable, Callable, Optional

import websockets
from websockets.exceptions import ConnectionClosed

from .config import settings

log = logging.getLogger(__name__)

SampleHandler = Callable[[dict], Awaitable[None]]


def _ws_url() -> str:
    return f"ws://{settings.shelly_host}{settings.shelly_ws_path}"


def _normalize_em(em: dict) -> dict:
    """Flatten the Shelly Pro 3EM 'em:0' status dict to a single-row sample."""
    return {
        "ts": time.time(),
        "a_current": em.get("a_current"),
        "b_current": em.get("b_current"),
        "c_current": em.get("c_current"),
        "a_voltage": em.get("a_voltage"),
        "b_voltage": em.get("b_voltage"),
        "c_voltage": em.get("c_voltage"),
        "a_power": em.get("a_act_power"),
        "b_power": em.get("b_act_power"),
        "c_power": em.get("c_act_power"),
        "a_pf": em.get("a_pf"),
        "b_pf": em.get("b_pf"),
        "c_pf": em.get("c_pf"),
        "total_power": em.get("total_act_power"),
        "total_current": em.get("total_current"),
    }


async def _subscribe(ws: websockets.WebSocketClientProtocol) -> None:
    # Ask the Shelly to push status notifications. The Pro 3EM emits NotifyStatus
    # automatically on the RPC websocket once connected; we send a no-op call to
    # confirm the channel is live.
    await ws.send(json.dumps({"id": 1, "method": "Shelly.GetDeviceInfo"}))


async def run_websocket_loop(on_sample: SampleHandler, stop: asyncio.Event) -> None:
    """Connect to the Shelly RPC websocket and stream samples until `stop` is set."""
    backoff = 1.0
    while not stop.is_set():
        url = _ws_url()
        try:
            log.info("Connecting to Shelly at %s", url)
            async with websockets.connect(url, ping_interval=20, ping_timeout=20) as ws:
                await _subscribe(ws)
                backoff = 1.0
                while not stop.is_set():
                    raw = await ws.recv()
                    try:
                        msg = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    sample = _extract_sample(msg)
                    if sample is not None:
                        await on_sample(sample)
        except (ConnectionClosed, OSError, asyncio.TimeoutError) as e:
            log.warning("Shelly websocket disconnected: %s; retry in %.1fs", e, backoff)
            try:
                await asyncio.wait_for(stop.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, 30.0)
        except Exception:
            log.exception("Unexpected error in shelly websocket loop")
            try:
                await asyncio.wait_for(stop.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, 30.0)


def _extract_sample(msg: dict) -> Optional[dict]:
    """Pull an em:0 reading out of a Shelly RPC message, if present."""
    # NotifyStatus → {"method":"NotifyStatus","params":{"em:0":{...}}}
    params = msg.get("params")
    if isinstance(params, dict):
        em = params.get("em:0")
        if isinstance(em, dict) and "a_act_power" in em:
            return _normalize_em(em)
    # Response to Shelly.GetStatus → {"result":{"em:0":{...}}}
    result = msg.get("result")
    if isinstance(result, dict):
        em = result.get("em:0")
        if isinstance(em, dict) and "a_act_power" in em:
            return _normalize_em(em)
    return None


async def fetch_status_once() -> Optional[dict]:
    """One-shot status fetch over websocket. Used for initial state."""
    url = _ws_url()
    try:
        async with websockets.connect(url, open_timeout=5) as ws:
            await ws.send(json.dumps({"id": 1, "method": "Shelly.GetStatus"}))
            for _ in range(10):
                raw = await asyncio.wait_for(ws.recv(), timeout=3)
                msg = json.loads(raw)
                sample = _extract_sample(msg)
                if sample is not None:
                    return sample
    except Exception as e:
        log.warning("fetch_status_once failed: %s", e)
    return None
