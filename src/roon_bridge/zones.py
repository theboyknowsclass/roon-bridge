"""Turning Roon's zone dicts into the API's shape, and volume levels into Roon units.

Roon outputs each have their own volume scale: 0 to 100, dB from -80 to 0, or
"incremental" (up and down only, no level). The API speaks 0 to 100 for every
output that has a level, and converts here.
"""

from typing import Any


class ZoneNotFound(LookupError):
    def __init__(self, wanted: str, known: list[str]) -> None:
        self.wanted = wanted
        self.known = known
        super().__init__(f"no zone {wanted!r}; known: {', '.join(known) or 'none reported yet'}")


class FixedVolume(ValueError):
    """The zone has no volume Roon can set."""


def find_zone(zones: list[dict[str, Any]], wanted: str) -> dict[str, Any]:
    """Match by case-insensitive display name, falling back to zone id."""
    key = wanted.strip().casefold()
    for zone in zones:
        if zone.get("display_name", "").casefold() == key:
            return zone
    for zone in zones:
        if zone.get("zone_id") == wanted:
            return zone
    raise ZoneNotFound(wanted, sorted(z.get("display_name", "") for z in zones))


def volume_outputs(zone: dict[str, Any]) -> list[dict[str, Any]]:
    """Outputs in the zone whose volume Roon controls."""
    return [o for o in zone.get("outputs", []) if o.get("volume")]


def has_level(volume: dict[str, Any]) -> bool:
    return (
        volume.get("type") != "incremental"
        and volume.get("value") is not None
        and volume.get("max", 0) > volume.get("min", 0)
    )


def to_level(volume: dict[str, Any]) -> float | None:
    """Roon units to 0-100, or None when the output has no level."""
    if not has_level(volume):
        return None
    lo, hi = volume["min"], volume["max"]
    return round((float(volume["value"]) - lo) / (hi - lo) * 100, 1)


def from_level(volume: dict[str, Any], level: float) -> float:
    """0-100 to Roon units, snapped to the output's step and kept in range."""
    lo, hi = volume["min"], volume["max"]
    raw = lo + max(0.0, min(100.0, level)) / 100 * (hi - lo)
    step = volume.get("step") or 0
    if step > 0:
        raw = lo + round((raw - lo) / step) * step
    raw = max(lo, min(hi, raw))
    # Integer steps get integer values, as roonapi does.
    return int(round(raw)) if float(step).is_integer() else round(raw, 4)


def zone_volume(zone: dict[str, Any]) -> dict[str, Any] | None:
    """The zone's volume, read from its first controllable output."""
    outputs = volume_outputs(zone)
    if not outputs:
        return None
    volume = outputs[0]["volume"]
    return {
        "level": to_level(volume),
        "muted": bool(volume.get("is_muted")),
        "type": volume.get("type"),
        "value": volume.get("value"),
        "min": volume.get("min"),
        "max": volume.get("max"),
        "step": volume.get("step"),
    }


def zone_to_dict(zone: dict[str, Any]) -> dict[str, Any]:
    now = zone.get("now_playing")
    lines = (now or {}).get("three_line") or {}
    return {
        "zone": zone.get("display_name"),
        "zone_id": zone.get("zone_id"),
        "state": zone.get("state"),
        "volume": zone_volume(zone),
        "outputs": [o.get("display_name") for o in zone.get("outputs", [])],
        "now_playing": (
            {
                "title": lines.get("line1"),
                "artist": lines.get("line2"),
                "album": lines.get("line3"),
                "length": now.get("length"),
                "image_key": now.get("image_key"),
            }
            if now
            else None
        ),
    }
