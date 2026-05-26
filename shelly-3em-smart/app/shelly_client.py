import asyncio
import json
import logging
import os
import time
from typing import Awaitable, Callable, Dict, Optional

import websockets
from websockets.exceptions import ConnectionClosed

from .config import settings

log = logging.getLogger(__name__)

SampleHandler = Callable[[dict], Awaitable[None]]

DEBUG_RAW = os.environ.get("DEBUG_SHELLY_RAW", "").lower() in ("1", "true", "yes")
GETSTATUS_POLL_S = float(os.environ.get("SHELLY_GETSTATUS_POLL_S", "2.0"))


def _ws_url() -> str:
    return f"ws://{settings.shelly_host}{settings.shelly_ws_path}"


class _SampleParser:
    """Handles both Pro 3EM modes:

      - 'EM' mode (3-phase): a single `em:0` component with a/b/c fields.
      - 'EM1' mode (3 independent meters, common for US split-phase): three
        `em1:0`, `em1:1`, `em1:2` components, each with their own fields.
    """

    def __init__(self) -> None:
        self._em1_partial: Dict[int, dict] = {}

    def parse(self, msg: dict) -> Optional[dict]:
        body = msg.get("params") if isinstance(msg.get("params"), dict) else msg.get("result")
        if not isinstance(body, dict):
            return None

        em = body.get("em:0")
        if isinstance(em, dict) and "a_act_power" in em:
            return self._from_em(em)

        had_em1 = False
        for key, val in body.items():
            if key.startswith("em1:") and isinstance(val, dict):
                try:
                    idx = int(key.split(":", 1)[1])
                except ValueError:
                    continue
                self._em1_partial[idx] = val
                had_em1 = True

        if had_em1 and self._em1_partial:
            return self._from_em1(self._em1_partial)
        return None

    @staticmethod
    def _from_em(em: dict) -> dict:
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

    @staticmethod
    def _from_em1(parts: Dict[int, dict]) -> dict:
        phase = {0: "a", 1: "b", 2: "c"}
        sample: dict = {"ts": time.time()}
        total_p = 0.0
        total_i = 0.0
        for idx, em1 in parts.items():
            p = phase.get(idx)
            if not p:
                continue
            sample[f"{p}_current"] = em1.get("current")
            sample[f"{p}_voltage"] = em1.get("voltage")
            sample[f"{p}_power"] = em1.get("act_power")
            sample[f"{p}_pf"] = em1.get("pf")
            if em1.get("act_power") is not None:
                total_p += float(em1["act_power"])
            if em1.get("current") is not None:
                total_i += float(em1["current"])
        sample["total_power"] = total_p
        sample["total_current"] = total_i
        return sample


async def _poll_loop(ws: websockets.WebSocketClientProtocol, stop: asyncio.Event) -> None:
    """Periodically send Shelly.GetStatus. Seeds the first sample (some firmwares
    wait for any RPC traffic before pushing NotifyStatus) and acts as a fallback
    if NotifyStatus isn't flowing."""
    req_id = 100
    while not stop.is_set():
        try:
            await ws.send(json.dumps({"id": req_id, "method": "Shelly.GetStatus"}))
            req_id += 1
        except ConnectionClosed:
            return
        try:
            await asyncio.wait_for(stop.wait(), timeout=GETSTATUS_POLL_S)
        except asyncio.TimeoutError:
            pass


async def run_websocket_loop(on_sample: SampleHandler, stop: asyncio.Event) -> None:
    backoff = 1.0
    while not stop.is_set():
        url = _ws_url()
        parser = _SampleParser()
        try:
            log.info("Connecting to Shelly at %s", url)
            async with websockets.connect(url, ping_interval=20, ping_timeout=20) as ws:
                log.info("Connected to Shelly; polling GetStatus every %.1fs", GETSTATUS_POLL_S)
                backoff = 1.0
                poller = asyncio.create_task(_poll_loop(ws, stop))
                try:
                    while not stop.is_set():
                        raw = await ws.recv()
                        try:
                            msg = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        if DEBUG_RAW:
                            log.info("RAW: %s", raw[:300])
                        sample = parser.parse(msg)
                        if sample is not None:
                            await on_sample(sample)
                finally:
                    poller.cancel()
                    try:
                        await poller
                    except (asyncio.CancelledError, Exception):
                        pass
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
