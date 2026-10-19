"""FastAPI service for one Roon zone.

One RoonConnection holds the websocket to the Core and keeps zone state current
from Roon's own pushes, so reads never touch the network. Every endpoint works
on the zone named by ROON_ZONE unless the request names another with `?zone=`.
"""

import asyncio
import json
import logging
import os
import re
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from importlib.resources import files
from pathlib import Path
from typing import Any, Literal, Protocol

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from .roon import NotConnectedError, RequestFailed, RoonConnection
from .zones import (
    FixedVolume,
    ZoneNotFound,
    find_zone,
    from_level,
    has_level,
    to_level,
    volume_outputs,
    zone_to_dict,
)

_LOGGER = logging.getLogger(__name__)

KEEPALIVE_SECONDS = 15.0
CONFIRM_SECONDS = 2.0
PENDING_SECONDS = 5.0
DEFAULT_ZONE = "Lounge"
_IMAGE_KEY_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")

MediaAction = Literal["play", "pause", "playpause", "stop", "next", "previous"]
ButtonAction = Literal[
    "play", "pause", "playpause", "stop", "next", "previous", "volume-up", "volume-down", "mute", "unmute"
]


class RoonClient(Protocol):
    """The slice of RoonConnection this service uses; tests supply a fake."""

    host: str | None
    port: int

    @property
    def ready(self) -> bool: ...
    @property
    def core_name(self) -> str | None: ...
    def zones(self) -> list[dict[str, Any]]: ...
    def subscribe(self, callback: Callable[[], None]) -> Callable[[], None]: ...
    async def async_start(self) -> None: ...
    async def async_stop(self) -> None: ...
    async def async_change_volume(self, output_id: str, how: str, value: float) -> None: ...
    async def async_mute(self, output_id: str, muted: bool) -> None: ...
    async def async_control(self, zone_id: str, control: str) -> None: ...
    async def async_image(self, image_key: str, size: int) -> tuple[bytes, str]: ...
    async def async_wait_for(
        self, predicate: Callable[[list[dict[str, Any]]], bool], timeout: float
    ) -> bool: ...


# --- request bodies -------------------------------------------------------


class VolumeBody(BaseModel):
    level: float = Field(ge=0, le=100)


class MuteBody(BaseModel):
    muted: bool


# --- helpers --------------------------------------------------------------


def _build_client() -> RoonConnection:
    host = os.environ.get("ROON_HOST", "").strip() or None
    port = int(os.environ.get("ROON_PORT", "9330"))
    data_dir = Path(os.environ.get("ROON_DATA_DIR", "/data"))
    return RoonConnection(host, port, data_dir)


def _volume_step() -> float:
    return float(os.environ.get("ROON_VOLUME_STEP", "2"))


class RequestedLevels:
    """The last volume sent to each output, until Roon reports it back.

    Some devices take a few seconds to confirm a change. Without this, a
    second "up" in that window would step from the old level and undo the first.
    """

    def __init__(self) -> None:
        self._sent: dict[str, tuple[float, float]] = {}

    def record(self, output_id: str, raw: float) -> None:
        self._sent[output_id] = (raw, time.monotonic())

    def current(self, output: dict[str, Any]) -> dict[str, Any]:
        """The output's volume, with a pending request standing in for the reported value."""
        volume = output["volume"]
        sent = self._sent.get(output["output_id"])
        if sent is None:
            return volume
        raw, at = sent
        if volume.get("value") == raw or time.monotonic() - at > PENDING_SECONDS:
            del self._sent[output["output_id"]]
            return volume
        return {**volume, "value": raw}


async def set_level(
    roon: RoonClient, zone: dict[str, Any], level: float, requested: RequestedLevels | None = None
) -> None:
    """Set every controllable output in the zone to `level` (0-100) and wait for Roon to report it."""
    outputs = [o for o in volume_outputs(zone) if has_level(o["volume"])]
    if not outputs:
        raise FixedVolume(f"zone {zone.get('display_name')!r} has no volume level Roon can set")
    targets = {o["output_id"]: from_level(o["volume"], level) for o in outputs}
    for output_id, raw in targets.items():
        await roon.async_change_volume(output_id, "absolute", raw)
        if requested is not None:
            requested.record(output_id, raw)

    def applied(zones: list[dict[str, Any]]) -> bool:
        for z in zones:
            for o in z.get("outputs", []):
                if o.get("output_id") in targets and (o.get("volume") or {}).get("value") != targets[o["output_id"]]:
                    return False
        return True

    await roon.async_wait_for(applied, CONFIRM_SECONDS)


async def step_level(
    roon: RoonClient, zone: dict[str, Any], delta: float, requested: RequestedLevels
) -> None:
    """Move the volume by `delta` on the 0-100 scale, or one step for up/down-only outputs."""
    outputs = volume_outputs(zone)
    if not outputs:
        raise FixedVolume(f"zone {zone.get('display_name')!r} has no volume Roon can change")
    first = requested.current(outputs[0])
    if has_level(first):
        await set_level(roon, zone, (to_level(first) or 0) + delta, requested)
        return
    for output in outputs:
        await roon.async_change_volume(output["output_id"], "relative_step", 1 if delta > 0 else -1)


async def set_muted(roon: RoonClient, zone: dict[str, Any], muted: bool) -> None:
    outputs = volume_outputs(zone)
    if not outputs:
        raise FixedVolume(f"zone {zone.get('display_name')!r} has no volume Roon can mute")
    for output in outputs:
        await roon.async_mute(output["output_id"], muted)

    ids = {o["output_id"] for o in outputs}

    def applied(zones: list[dict[str, Any]]) -> bool:
        return all(
            bool((o.get("volume") or {}).get("is_muted")) == muted
            for z in zones
            for o in z.get("outputs", [])
            if o.get("output_id") in ids
        )

    await roon.async_wait_for(applied, CONFIRM_SECONDS)


# --- app ------------------------------------------------------------------


def create_app(client: RoonClient | None = None, default_zone: str | None = None) -> FastAPI:
    zone_default = default_zone or os.environ.get("ROON_ZONE", "").strip() or DEFAULT_ZONE

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        roon: RoonClient = client if client is not None else _build_client()
        _LOGGER.info("default zone is %r", zone_default)
        # The service comes up even while Roon is unreachable or the extension
        # is not enabled yet; /healthz says which.
        await roon.async_start()
        app.state.roon = roon
        app.state.requested = RequestedLevels()
        try:
            yield
        finally:
            await roon.async_stop()

    app = FastAPI(title="Roon Bridge", version="0.1.0", lifespan=lifespan)

    def roon_of(request: Request) -> RoonClient:
        return request.app.state.roon

    def requested_of(request: Request) -> RequestedLevels:
        return request.app.state.requested

    def zone_of(request: Request, zone: str | None) -> dict[str, Any]:
        roon = roon_of(request)
        if not roon.ready:
            raise NotConnectedError(
                "not registered with a Roon Core; check the log, and that the "
                "extension is enabled in Roon under Settings > Extensions"
            )
        return find_zone(roon.zones(), zone or zone_default)

    def state_of(request: Request, zone: str | None) -> dict[str, Any]:
        return zone_to_dict(zone_of(request, zone))

    ZoneParam = Query(None, description=f"Zone name or id; defaults to {zone_default!r}")

    @app.exception_handler(NotConnectedError)
    async def _not_connected(_: Request, err: NotConnectedError) -> JSONResponse:
        return JSONResponse({"detail": str(err)}, status_code=503)

    @app.exception_handler(ZoneNotFound)
    async def _no_zone(_: Request, err: ZoneNotFound) -> JSONResponse:
        return JSONResponse({"detail": str(err), "known": err.known}, status_code=404)

    @app.exception_handler(FixedVolume)
    async def _fixed(_: Request, err: FixedVolume) -> JSONResponse:
        return JSONResponse({"detail": str(err)}, status_code=409)

    @app.exception_handler(RequestFailed)
    async def _failed(_: Request, err: RequestFailed) -> JSONResponse:
        return JSONResponse({"detail": str(err)}, status_code=502)

    # --- page and health ----------------------------------------------

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def index() -> str:
        return (files("roon_bridge") / "static" / "index.html").read_text(encoding="utf-8")

    @app.get("/api/image/{image_key}", include_in_schema=False)
    async def image(
        request: Request, image_key: str, size: int = Query(300, ge=32, le=1024)
    ) -> Response:
        """Album art, proxied so the page works where the Core itself isn't reachable."""
        if not _IMAGE_KEY_RE.fullmatch(image_key):
            raise HTTPException(400, "bad image key")
        body, content_type = await roon_of(request).async_image(image_key, size)
        return Response(body, media_type=content_type, headers={"Cache-Control": "max-age=86400"})

    @app.get("/healthz")
    async def healthz(request: Request) -> dict[str, Any]:
        roon = roon_of(request)
        return {
            "status": "ok",
            "roon_host": roon.host,
            "roon_ready": roon.ready,
            "core": roon.core_name,
            "default_zone": zone_default,
        }

    # --- REST API -----------------------------------------------------

    @app.get("/api/zones")
    async def get_zones(request: Request) -> list[dict[str, Any]]:
        roon = roon_of(request)
        if not roon.ready:
            zone_of(request, None)  # raises the 503
        return [zone_to_dict(z) for z in roon.zones()]

    @app.get("/api/state")
    async def get_state(request: Request, zone: str | None = ZoneParam) -> dict[str, Any]:
        return state_of(request, zone)

    @app.get("/api/volume")
    async def get_volume(request: Request, zone: str | None = ZoneParam) -> dict[str, Any]:
        """`level` is 0-100, or null for a zone with fixed or up/down-only volume."""
        volume = state_of(request, zone)["volume"]
        return {"level": volume["level"] if volume else None, "muted": bool(volume and volume["muted"])}

    @app.put("/api/volume")
    async def put_volume(request: Request, body: VolumeBody, zone: str | None = ZoneParam) -> dict[str, Any]:
        await set_level(roon_of(request), zone_of(request, zone), body.level, requested_of(request))
        return state_of(request, zone)

    @app.post("/api/volume/{direction}")
    async def post_volume_step(
        request: Request,
        direction: Literal["up", "down"],
        zone: str | None = ZoneParam,
        by: float | None = Query(None, gt=0, le=100, description="Step on the 0-100 scale; default ROON_VOLUME_STEP"),
    ) -> dict[str, Any]:
        delta = by if by is not None else _volume_step()
        await step_level(
            roon_of(request), zone_of(request, zone), delta if direction == "up" else -delta, requested_of(request)
        )
        return state_of(request, zone)

    @app.put("/api/mute")
    async def put_mute(request: Request, body: MuteBody, zone: str | None = ZoneParam) -> dict[str, Any]:
        await set_muted(roon_of(request), zone_of(request, zone), body.muted)
        return state_of(request, zone)

    @app.post("/api/media/{action}")
    async def post_media(request: Request, action: MediaAction, zone: str | None = ZoneParam) -> dict[str, Any]:
        z = zone_of(request, zone)
        await roon_of(request).async_control(z["zone_id"], action)
        return state_of(request, zone)

    @app.get("/api/events")
    async def events(request: Request, zone: str | None = ZoneParam) -> StreamingResponse:
        """Server-sent events: the zone's state on connect and whenever it changes."""
        roon = roon_of(request)
        loop = asyncio.get_running_loop()
        # Size 1, latest wins: a slow reader skips intermediate states rather
        # than falling behind.
        queue: asyncio.Queue[None] = asyncio.Queue(maxsize=1)

        def poke() -> None:
            if not queue.full():
                queue.put_nowait(None)

        def current() -> dict[str, Any]:
            try:
                return state_of(request, zone)
            except (NotConnectedError, ZoneNotFound) as err:
                return {"error": str(err)}

        async def stream() -> AsyncIterator[str]:
            # Roon calls back on its own thread; hop onto the event loop.
            unsubscribe = roon.subscribe(lambda: loop.call_soon_threadsafe(poke))
            try:
                last = current()
                yield f"data: {json.dumps(last)}\n\n"
                while not await request.is_disconnected():
                    try:
                        await asyncio.wait_for(queue.get(), KEEPALIVE_SECONDS)
                    except TimeoutError:
                        yield ": keepalive\n\n"
                        continue
                    state = current()
                    # Roon reports zone and output changes separately, and
                    # often both for one change; send each new state once.
                    if state != last:
                        last = state
                        yield f"data: {json.dumps(state)}\n\n"
            finally:
                unsubscribe()

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # --- one-shot actions, for buttons ---------------------------------
    #
    # A Flic button sends a fixed request with no body, and some setups can
    # only send GET, so these are bare URLs that accept either.

    @app.api_route("/api/actions/{action}", methods=["GET", "POST"])
    async def action(request: Request, action: ButtonAction, zone: str | None = ZoneParam) -> dict[str, Any]:
        roon = roon_of(request)
        z = zone_of(request, zone)
        if action == "volume-up":
            await step_level(roon, z, _volume_step(), requested_of(request))
        elif action == "volume-down":
            await step_level(roon, z, -_volume_step(), requested_of(request))
        elif action in ("mute", "unmute"):
            await set_muted(roon, z, action == "mute")
        else:
            await roon.async_control(z["zone_id"], action)
        return state_of(request, zone)

    return app


app = create_app()
