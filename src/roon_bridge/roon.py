"""One long-lived connection to a Roon Core.

roonapi is synchronous: it runs the websocket on a thread of its own, keeps its
`zones` and `outputs` dicts current from Roon's pushes, and blocks the caller
on every request. This wrapper owns that connection, saves the pairing token so
the extension only has to be enabled in Roon once, and offers async methods
that run the blocking calls on a worker thread.
"""

import asyncio
import copy
import json
import logging
import threading
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from roonapi import RoonApi, RoonDiscovery

_LOGGER = logging.getLogger(__name__)

APPINFO = {
    "extension_id": "com.theboyknowsclass.roon-bridge",
    "display_name": "Roon Bridge",
    "display_version": "0.1.0",
    "publisher": "theboyknowsclass",
    "email": "roon-bridge@users.noreply.github.com",
    "website": "https://github.com/theboyknowsclass/roon-bridge",
}

DISCOVERY_RETRY_SECONDS = 10.0
TOKEN_POLL_SECONDS = 1.0
APPROVAL_HINT_AFTER_SECONDS = 5.0
REFRESH_RETRY_SECONDS = 30.0


class RoonError(Exception):
    """Base class for errors talking to Roon."""


class NotConnectedError(RoonError):
    """Not connected to a Core, or the extension is not enabled there yet."""


class RequestFailed(RoonError):
    """Roon answered a request with something other than Success."""


class RoonConnection:
    """Connects in the background and reconnects by itself (roonapi does that part)."""

    def __init__(self, host: str | None, port: int, data_dir: Path) -> None:
        self.host = host
        self.port = port
        self._state_file = data_dir / "roon.json"
        self._api: RoonApi | None = None
        self._stopping = threading.Event()
        self._changed = threading.Condition()
        self._subscribers: list[Callable[[], None]] = []
        self._thread: threading.Thread | None = None

    # --- state ---------------------------------------------------------

    @property
    def ready(self) -> bool:
        """Connected and registered, i.e. the extension is enabled in Roon."""
        return self._api is not None and bool(self._api.ready)

    @property
    def core_name(self) -> str | None:
        return self._api.core_name if self._api is not None else None

    def zones(self) -> list[dict[str, Any]]:
        """A copy of every zone, with each output's latest volume merged in."""
        api = self._api
        if api is None:
            return []
        # Skip half-known zones (see _refresh) rather than show them nameless.
        zones = copy.deepcopy([z for z in api.zones.values() if "display_name" in z])
        outputs = api.outputs
        for zone in zones:
            # The outputs subscription can be fresher than the copy inside the
            # zone, and it is what roonapi itself reads volume from.
            zone["outputs"] = [
                copy.deepcopy(outputs.get(o["output_id"], o)) for o in zone.get("outputs", [])
            ]
        return zones

    def subscribe(self, callback: Callable[[], None]) -> Callable[[], None]:
        """Call `callback` (on Roon's thread) whenever a zone or output changes."""
        self._subscribers.append(callback)
        return lambda: self._subscribers.remove(callback)

    # --- lifecycle -----------------------------------------------------

    async def async_start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="roon", daemon=True)
        self._thread.start()

    async def async_stop(self) -> None:
        self._stopping.set()
        if self._api is not None:
            await asyncio.to_thread(self._api.stop)

    def _run(self) -> None:
        saved = self._load()
        host, port = self.host, self.port
        while host is None and not self._stopping.is_set():
            _LOGGER.info("ROON_HOST not set; searching the network for a Roon Core")
            host, found_port = RoonDiscovery(saved.get("core_id")).first()
            if host:
                port = found_port
                self.host, self.port = host, port
                _LOGGER.info("discovered Roon Core at %s:%s", host, port)
            else:
                _LOGGER.warning("no Roon Core found; set ROON_HOST. Retrying")
                self._stopping.wait(DISCOVERY_RETRY_SECONDS)
        if self._stopping.is_set():
            return

        _LOGGER.info("connecting to Roon Core at %s:%s", host, port)
        api = RoonApi(APPINFO, saved.get("token"), host, port, blocking_init=False)
        # Seek updates arrive every second while playing; nothing here needs them.
        api.register_state_callback(self._on_change, event_filter=["zones_changed", "outputs_changed"])
        self._api = api
        self._watch_registration(api, saved)

    def _watch_registration(self, api: RoonApi, saved: dict[str, Any]) -> None:
        """Log readiness changes and save the token whenever Roon issues a new one."""
        last_ready: bool | None = None
        waited = 0.0
        next_refresh = 0.0
        hinted = False
        while not self._stopping.wait(TOKEN_POLL_SECONDS):
            waited += TOKEN_POLL_SECONDS
            if api.token and api.token != saved.get("token"):
                saved = {"token": api.token, "core_id": api.core_id}
                self._save(saved)
            ready = bool(api.ready)
            # On every (re)registration, and at most every 30s while zones are incomplete.
            if ready and (ready != last_ready or (self._incomplete(api) and waited >= next_refresh)):
                next_refresh = waited + REFRESH_RETRY_SECONDS
                self._refresh(api)
            if ready != last_ready:
                last_ready = ready
                if ready:
                    _LOGGER.info("registered with Roon Core %r", api.core_name)
                    self._notify()
                elif waited > TOKEN_POLL_SECONDS:
                    _LOGGER.warning("lost the Roon Core; roonapi reconnects by itself")
            if not ready and not hinted and waited >= APPROVAL_HINT_AFTER_SECONDS:
                hinted = True
                _LOGGER.warning(
                    "not registered yet: in Roon, open Settings > Extensions and enable %r",
                    APPINFO["display_name"],
                )

    @staticmethod
    def _incomplete(api: RoonApi) -> bool:
        return not api.zones or any("display_name" not in z for z in api.zones.values())

    def _refresh(self, api: RoonApi) -> None:
        """Fetch every zone and output outright.

        roonapi sends each subscribe request before it starts listening for the
        reply, so on a fast network Roon's first reply, the one listing every
        zone, can be dropped. Later updates are partial, and on their own leave
        zones with no name or outputs. Asking again fixes that.
        """
        # Private, but stable in the exactly pinned roonapi.
        zones = api._get_zones()
        outputs = api._get_outputs()
        if zones:
            api._zones.clear()
            api._zones.update(zones)
        if outputs:
            api._outputs.clear()
            api._outputs.update(outputs)
        _LOGGER.info("loaded %d zones from Roon", len(zones))
        self._notify()

    def _on_change(self, _event: str, _changed_ids: list[str]) -> None:
        self._notify()

    def _notify(self) -> None:
        with self._changed:
            self._changed.notify_all()
        for callback in list(self._subscribers):
            try:
                callback()
            except Exception:  # a broken listener must not stop Roon's thread
                _LOGGER.exception("zone listener failed")

    def _load(self) -> dict[str, Any]:
        try:
            return json.loads(self._state_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            _LOGGER.exception("could not read %s; pairing with Roon again", self._state_file)
            return {}

    def _save(self, saved: dict[str, Any]) -> None:
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            self._state_file.write_text(json.dumps(saved), encoding="utf-8")
            _LOGGER.info("saved Roon pairing to %s", self._state_file)
        except OSError:
            _LOGGER.exception(
                "could not save the Roon pairing to %s; it will need enabling again after a restart",
                self._state_file,
            )

    # --- requests ------------------------------------------------------

    async def async_change_volume(self, output_id: str, how: str, value: float) -> None:
        """`how` is Roon's: 'absolute', 'relative' or 'relative_step', in the output's own units."""
        await self._call(lambda api: api.change_volume_raw(output_id, value, how))

    async def async_mute(self, output_id: str, muted: bool) -> None:
        await self._call(lambda api: api.mute(output_id, muted))

    async def async_control(self, zone_id: str, control: str) -> None:
        await self._call(lambda api: api.playback_control(zone_id, control))

    async def async_image(self, image_key: str, size: int) -> tuple[bytes, str]:
        """Album art from the Core's image server, as (body, content type)."""
        if not self.host:
            raise NotConnectedError("no Roon Core address yet")
        query = urllib.parse.urlencode({"scale": "fit", "width": size, "height": size})
        url = f"http://{self.host}:{self.port}/api/image/{urllib.parse.quote(image_key)}?{query}"

        def fetch() -> tuple[bytes, str]:
            try:
                with urllib.request.urlopen(url, timeout=5) as res:
                    return res.read(), res.headers.get_content_type()
            except (OSError, ValueError) as err:
                raise RequestFailed(f"image {image_key!r}: {err}") from err

        return await asyncio.to_thread(fetch)

    async def async_wait_for(
        self, predicate: Callable[[list[dict[str, Any]]], bool], timeout: float
    ) -> bool:
        """Wait until `predicate(zones)` holds, or `timeout` passes. Returns which."""

        def wait() -> bool:
            with self._changed:
                return self._changed.wait_for(lambda: predicate(self.zones()), timeout)

        return await asyncio.to_thread(wait)

    async def _call(self, request: Callable[[RoonApi], Any]) -> None:
        api = self._api
        if api is None or not api.ready:
            raise NotConnectedError(
                "not registered with a Roon Core; check the log, and that the "
                "extension is enabled in Roon under Settings > Extensions"
            )
        try:
            result = await asyncio.to_thread(request, api)
        except KeyError as err:
            raise RequestFailed(f"Roon does not know {err}") from err
        # roonapi hands back the raw reply, "MOO/1 COMPLETE Success" when it
        # worked, and None when the request failed or timed out.
        if not (isinstance(result, str) and "Success" in result):
            raise RequestFailed(f"Roon did not accept the request: {result!r}")
