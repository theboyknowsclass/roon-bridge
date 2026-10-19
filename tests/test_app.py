from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient

from roon_bridge.app import create_app
from roon_bridge.zones import from_level, to_level


def make_zone(name: str, zone_id: str, volume: dict[str, Any] | None, state: str = "playing") -> dict[str, Any]:
    output: dict[str, Any] = {"output_id": f"{zone_id}-out", "display_name": name}
    if volume is not None:
        output["volume"] = volume
    return {
        "zone_id": zone_id,
        "display_name": name,
        "state": state,
        "outputs": [output],
        "now_playing": {
            "three_line": {"line1": "Song", "line2": "Artist", "line3": "Album"},
            "length": 200,
            "image_key": "img",
        },
    }


class FakeRoon:
    """Applies requests to its own zones, as Roon would, and records them."""

    def __init__(self, zones: list[dict[str, Any]], ready: bool = True) -> None:
        self.host = "192.168.1.10"
        self.port = 9330
        self._ready = ready
        self._zones = zones
        self.calls: list[tuple] = []
        self._subscribers: list[Callable[[], None]] = []

    @property
    def ready(self) -> bool:
        return self._ready

    @property
    def core_name(self) -> str | None:
        return "Fake Core"

    def zones(self) -> list[dict[str, Any]]:
        return self._zones

    def subscribe(self, callback: Callable[[], None]) -> Callable[[], None]:
        self._subscribers.append(callback)
        return lambda: self._subscribers.remove(callback)

    async def async_start(self) -> None:
        pass

    async def async_stop(self) -> None:
        pass

    def _output(self, output_id: str) -> dict[str, Any]:
        return next(o for z in self._zones for o in z["outputs"] if o["output_id"] == output_id)

    async def async_change_volume(self, output_id: str, how: str, value: float) -> None:
        self.calls.append(("volume", output_id, how, value))
        volume = self._output(output_id)["volume"]
        if how == "absolute":
            volume["value"] = value

    async def async_mute(self, output_id: str, muted: bool) -> None:
        self.calls.append(("mute", output_id, muted))
        self._output(output_id)["volume"]["is_muted"] = muted

    async def async_control(self, zone_id: str, control: str) -> None:
        self.calls.append(("control", zone_id, control))

    async def async_image(self, image_key: str, size: int) -> tuple[bytes, str]:
        return f"img:{image_key}:{size}".encode(), "image/jpeg"

    async def async_wait_for(self, predicate, timeout: float) -> bool:
        return predicate(self._zones)


DB = {"type": "db", "min": -80, "max": 0, "step": 1, "value": -40, "is_muted": False}
NUMBER = {"type": "number", "min": 0, "max": 100, "step": 1, "value": 30, "is_muted": False}


@pytest.fixture
def fake() -> FakeRoon:
    return FakeRoon(
        [
            make_zone("Lounge", "z1", dict(DB)),
            make_zone("Kitchen", "z2", dict(NUMBER), state="paused"),
            make_zone("Fixed DAC", "z3", None),
            make_zone("Amp", "z4", {"type": "incremental", "is_muted": False}),
        ]
    )


@pytest.fixture
def client(fake: FakeRoon):
    with TestClient(create_app(fake, default_zone="Lounge")) as c:
        yield c


def test_level_conversion_round_trips():
    assert to_level(DB) == 50.0
    assert from_level(DB, 50) == -40
    assert from_level(DB, 101) == 0
    assert from_level({**DB, "step": 0.5}, 33.3) == -53.5
    assert to_level({"type": "incremental"}) is None


def test_state_defaults_to_configured_zone(client):
    body = client.get("/api/state").json()
    assert body["zone"] == "Lounge"
    assert body["volume"]["level"] == 50.0
    assert body["now_playing"]["title"] == "Song"


def test_zone_query_is_case_insensitive(client):
    assert client.get("/api/state", params={"zone": "kitchen"}).json()["zone_id"] == "z2"


def test_unknown_zone_lists_known(client):
    r = client.get("/api/state", params={"zone": "Bathroom"})
    assert r.status_code == 404
    assert "Kitchen" in r.json()["known"]


def test_put_volume_converts_to_output_units(client, fake):
    body = client.put("/api/volume", json={"level": 75}).json()
    assert fake.calls == [("volume", "z1-out", "absolute", -20)]
    assert body["volume"]["level"] == 75.0


def test_get_volume(client):
    assert client.get("/api/volume", params={"zone": "Kitchen"}).json() == {"level": 30.0, "muted": False}


def test_volume_up_uses_step(client, fake, monkeypatch):
    monkeypatch.setenv("ROON_VOLUME_STEP", "5")
    client.post("/api/volume/up")
    assert fake.calls == [("volume", "z1-out", "absolute", -36)]


def test_volume_down_on_incremental_output_steps(client, fake):
    client.post("/api/volume/down", params={"zone": "Amp"})
    assert fake.calls == [("volume", "z4-out", "relative_step", -1)]


def test_fixed_volume_is_409(client):
    assert client.put("/api/volume", params={"zone": "Fixed DAC"}, json={"level": 10}).status_code == 409


def test_volume_out_of_range_is_422(client):
    assert client.put("/api/volume", json={"level": 120}).status_code == 422


def test_mute(client, fake):
    body = client.put("/api/mute", json={"muted": True}).json()
    assert fake.calls == [("mute", "z1-out", True)]
    assert body["volume"]["muted"] is True


def test_media_controls_target_zone(client, fake):
    client.post("/api/media/playpause")
    client.post("/api/media/next", params={"zone": "Kitchen"})
    assert fake.calls == [("control", "z1", "playpause"), ("control", "z2", "next")]


def test_button_actions_accept_get(client, fake):
    assert client.get("/api/actions/previous").status_code == 200
    assert client.get("/api/actions/mute").status_code == 200
    assert fake.calls == [("control", "z1", "previous"), ("mute", "z1-out", True)]


def test_not_registered_is_503():
    with TestClient(create_app(FakeRoon([], ready=False))) as c:
        assert c.get("/api/state").status_code == 503
        assert c.get("/healthz").json()["roon_ready"] is False


class SlowRoon(FakeRoon):
    """A device that takes its time: Roon hasn't reported the new level yet."""

    async def async_change_volume(self, output_id: str, how: str, value: float) -> None:
        self.calls.append(("volume", output_id, how, value))


def test_repeated_steps_build_on_unconfirmed_level():
    slow = SlowRoon([make_zone("Lounge", "z1", dict(DB))])
    with TestClient(create_app(slow, default_zone="Lounge")) as c:
        c.post("/api/volume/up", params={"by": 5})
        c.post("/api/volume/up", params={"by": 5})
    assert [call[3] for call in slow.calls] == [-36, -32]


def test_page_is_served(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Roon Bridge" in r.text


def test_image_proxy(client):
    r = client.get("/api/image/abc123")
    assert r.content == b"img:abc123:300"
    assert r.headers["content-type"] == "image/jpeg"
    assert client.get("/api/image/..%2Fetc").status_code in (400, 404)
