# openhost-unciv

[Unciv](https://github.com/yairm210/Unciv) (open-source Civ V remake)
multiplayer server, packaged for OpenHost.

## What this is

This hosts **UncivServer** — the small HTTP service that stores Unciv
multiplayer game saves. It is **not** a website you log into and it does
**not** run the game itself. It's the backend that Unciv **game clients**
(desktop / Android) talk to so you and your friends can play online
multiplayer without relying on the flaky shared Dropbox-backed default
server.

Bundled:

- **UncivServer.jar** — the upstream Ktor/Netty game-save API
  (pre-built, downloaded from the pinned Unciv release).
- **auth_proxy.py** — a small front proxy that serves the OpenHost
  health probe, an owner-only landing page, and transparently forwards
  the game API to UncivServer.

## How to use it

1. Open the app's URL in your browser while signed in to your OpenHost
   zone. You (the owner) get a landing page showing the **server
   address** to use.
2. In Unciv, go to `Main Menu → Options → Multiplayer`, set **Server
   address** to that URL, and click **Check connection to server** — you
   should see **Success!**
3. Share the same URL with your friends so their Unciv clients point at
   the same server.
4. Start an `Online multiplayer` game and share the game ID, as usual
   (see Unciv's in-game multiplayer flow).

## Auth model

Unciv multiplayer has an unusual shape: the thing that connects to the
server is a **game client**, not a browser, so there is no OpenHost SSO
login to perform. Access is therefore split:

- **Owner landing page (`/`)** — gated by OpenHost SSO. Only the zone
  owner sees the setup instructions and server URL. Anonymous visitors
  to `/` are bounced to the zone login.
- **Game API (`/isalive`, `/files/`, `/auth`, `/chat`)** — **public**,
  because the Unciv clients that call these cannot do the browser
  zone_auth flow. This is inherent to how every self-hosted Unciv server
  works: anyone with the URL can reach the API. UncivServer's optional
  **per-user "auth v1"** (enabled by default here) lets each player set a
  password in Unciv's multiplayer options that guards writes to *their
  own* save slot.

If you want a fully open, no-password server for a trusted group, set the
env var `UncivServerAuth=false`.

## Architecture

```
  Unciv game    ──HTTPS──▶ OpenHost router
  client (or                (public paths pass through; SSO-gates only /)
  browser at /)         ──▶ container :8080  (auth_proxy.py)
                              │
                              ├─ /_healthz  → local 200 (readiness probe)
                              ├─ /          → owner-only landing page
                              └─ everything else (game API + /chat WS)
                                     → 127.0.0.1:8081  (UncivServer)
                                          ↕
                                       MultiplayerFiles/  (game saves,
                                       under $OPENHOST_APP_DATA_DIR)
```

## Persistence

`$OPENHOST_APP_DATA_DIR` (typically `/data/app_data/unciv/`) holds:

- `MultiplayerFiles/` — one file per multiplayer game save. This is the
  entire durable state; back this up to preserve in-progress games.

No passwords or secrets are written to disk. UncivServer's per-user
"auth v1" passwords are kept in memory only (they reset on restart, and
clients re-send them), so nothing under the data dir is a usable
credential.

## Resources

1 GiB RAM, 1 CPU. UncivServer is a lightweight JVM file-storage service
plus a tiny Python proxy; save up/download and the chat WebSocket are
not demanding.

## Known limitations / scope cuts

- **The server does not run the game.** Players still need the Unciv
  client on their own devices; this only stores the shared game state.
- **Per-user passwords are in-memory.** UncivServer's auth v1 keeps
  passwords in RAM, so they reset on container restart. Clients re-send
  them automatically, so in practice this is transparent, but it means
  the server is not a long-term credential store.
- **Old saves are not auto-pruned.** UncivServer never deletes save
  files on its own; a very long-lived server accumulates files under
  `MultiplayerFiles/`.

## Files

- `openhost.toml` — OpenHost manifest.
- `Dockerfile` — Temurin JRE base; downloads the pinned
  `UncivServer.jar`.
- `auth_proxy.py` — health + owner landing page + transparent forward.
- `start.sh` — bash supervisor (UncivServer + proxy; `wait -n`).
