# roon-bridge

A small HTTP service for one [Roon](https://roon.app) zone: volume, mute,
play/pause and track skipping, plus a live event stream of the zone's state.
It is meant for things that can fetch a URL or hold a socket open, like a Flic
button, a Flic Twist script on a Flic Hub, or a home automation system.

It registers with your Roon Core as an extension, holds one connection open,
keeps the zones current from Roon's own pushes, and exposes them over HTTP:

- a phone-friendly web page at `/`: pick a zone, see what's playing, play/pause, skip, volume and mute
- a JSON API at `/api/...` (interactive docs at `/docs`)
- one-shot URLs for buttons (`/api/actions/playpause` and friends)
- server-sent events at `/api/events`, so a dial can follow volume changes made elsewhere

Every endpoint works on the zone named by `ROON_ZONE` (default
`Lounge`). Add `?zone=Kitchen` to any request to use another one.

The Roon side is handled by [pyroon](https://github.com/pavoni/pyroon), the
library Home Assistant's Roon integration uses.

## Run it

### Docker (recommended)

```bash
docker run -d --name roon-bridge -p 8080:8080 \
  -e ROON_HOST=192.168.1.20 \
  -e ROON_ZONE="Lounge" \
  -v roon-bridge-data:/data \
  --restart unless-stopped \
  ghcr.io/theboyknowsclass/roon-bridge:latest
```

Or with the included [compose.yml](compose.yml): copy
[.env.example](.env.example) to `.env`, set your Roon Core's address and zone,
then:

```bash
docker compose up -d
```

### Enable the extension in Roon (once)

The first time it connects, open Roon and go to **Settings > Extensions**,
then enable **Roon Bridge**. Until you do, the API answers `503` and the log
says it is waiting. Roon then issues a token, which the service saves in
`/data/roon.json`. Keep `/data` on a volume and you never need to do this again.

### Locally

```bash
python -m venv .venv
.venv/bin/pip install -e .
ROON_HOST=192.168.1.20 ROON_DATA_DIR=./data .venv/bin/roon-bridge
```

Images are built for `linux/amd64` and `linux/arm64` on every push to `main`
(`:latest`, `:sha-<short>`) and on `v*` tags (`:1.2.3`).

## Configuration

| Variable | Default | |
| --- | --- | --- |
| `ROON_HOST` | *(discover)* | The Roon Core's IP address. Strongly recommended; see below. |
| `ROON_PORT` | `9330` | The Core's API port. Roon 2.x uses 9330; older cores used 9100. |
| `ROON_ZONE` | `Lounge` | Zone used when a request doesn't name one. Matched case-insensitively. |
| `ROON_VOLUME_STEP` | `2` | Step for volume up/down, on the 0–100 scale. |
| `ROON_DATA_DIR` | `/data` | Where the Roon pairing token is kept. |
| `ROON_BRIDGE_PORT` | `8080` | Host port in [compose.yml](compose.yml). |
| `PORT` | `8080` | Port the service listens on inside the container. |
| `BIND_HOST` | `0.0.0.0` | Address the service listens on. |
| `LOG_LEVEL` | `info` | |

If `ROON_HOST` is unset, the service looks for a Core by multicast at startup.
Multicast does not cross Docker's default bridge network, so discovery inside a
container needs `network_mode: host`. Setting the address avoids the problem;
the service only makes outgoing connections to the Core.

## Volume

Roon outputs each have their own volume scale: 0 to 100, decibels (often
−80 to 0), or "incremental", which only goes up and down. The API uses 0–100
for every output that has a level, converts to the output's own units, and
snaps to its step. The raw values are in `/api/state` too.

- A zone whose volume isn't controlled by Roon (fixed volume) answers `409`.
- An incremental output has a `level` of `null`. `up` and `down` still work,
  one step at a time.
- A zone with several outputs (a group) has every output set to the same
  level. The reported level is the first output's.

Writes return once Roon reports the new level, or after 2 seconds if it
doesn't, with the state as it is then.

## Flic button

Add an **Internet Request** action to the button, in the Flic app or on a Flic
Hub, with method `GET` (or `POST`) and a URL like:

```
http://<docker-host>:8080/api/actions/playpause
```

Actions: `play`, `pause`, `playpause`, `stop`, `next`, `previous`,
`volume-up`, `volume-down`, `mute`, `unmute`. Add `?zone=Kitchen` for a zone
other than the default.

The request is sent by whatever runs the Flic action. For the phone app that
means the phone has to be on your home network.

## API

| Method | Path | Body | |
| --- | --- | --- | --- |
| `GET` | `/api/zones` | | Every zone, in the same shape as `/api/state` |
| `GET` | `/api/state` | | Zone, playback state, volume, now playing |
| `GET` | `/api/volume` | | `{"level": 42.5, "muted": false}` |
| `PUT` | `/api/volume` | `{"level": 30}` | 0–100 |
| `POST` | `/api/volume/{up,down}` | | Optional `?by=5`; default `ROON_VOLUME_STEP` |
| `PUT` | `/api/mute` | `{"muted": true}` | |
| `POST` | `/api/media/{play,pause,playpause,stop,next,previous}` | | `previous` goes to the start of the track first, as in Roon |
| `GET`/`POST` | `/api/actions/{action}` | | For buttons; see above |
| `GET` | `/api/events` | | Server-sent events: the zone's state on connect and on every change |
| `GET` | `/healthz` | | Liveness, plus whether Roon is registered |

All of them take an optional `?zone=` (name or zone id).

| Status | Meaning |
| --- | --- |
| `404` | No such zone. The body lists the known ones. |
| `409` | The zone has no volume Roon can change. |
| `502` | Roon refused the request or didn't answer. |
| `503` | Not connected to a Core, or the extension isn't enabled in Roon yet. |

## Security

There is no authentication. Anyone who can reach the port can control your
Roon zones. Keep it on your LAN, or put it behind something that authenticates.

## Development

```bash
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest
```

The tests run the API against a fake Roon, so they need no Core.

The page's album art comes through the service (`/api/image/{key}`), so
phones that can't reach the Core directly still get it.

## License

MIT
