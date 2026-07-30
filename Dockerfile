# openhost-unciv
#
# Unciv (open-source Civ V remake) multiplayer server, packaged for
# OpenHost.
#
# What this actually hosts: UncivServer — the small Ktor/Netty HTTP
# service that stores multiplayer game saves.  It is NOT a website you
# log into; it is a headless API that the Unciv *game client* (desktop
# / Android) talks to.  The whole point of self-hosting it is so the
# zone owner and their friends can play online multiplayer without
# relying on the flaky shared Dropbox-backed default server.
#
# API surface (see server/src/.../UncivServer.kt upstream):
#   GET  /isalive            — handshake / health (unauth)
#   PUT  /files/{name}       — upload a game save   (optional basic auth)
#   GET  /files/{name}       — download a game save (optional basic auth)
#   GET  /auth, PUT /auth    — per-user save password management
#   WS   /chat               — realtime chat / turn notifications
#
# Because the game clients that hit these endpoints cannot perform
# OpenHost's browser SSO flow, the API is served publicly (see
# openhost.toml).  auth_proxy.py adds an owner-only landing page at /
# that shows the server URL to paste into the Unciv client, serves the
# OpenHost health probe, and transparently forwards the game API to
# UncivServer on loopback.
#
# We download the pre-built UncivServer.jar from the upstream release
# rather than run the (very large, libGDX/Gradle) source build.

FROM eclipse-temurin:21-jre-jammy

# Pinned Unciv release.  UncivServer.jar is a self-contained fat jar.
ARG UNCIV_VERSION=4.21.4
ARG UNCIV_SERVER_SHA256=""

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends curl ca-certificates python3; \
    rm -rf /var/lib/apt/lists/*; \
    curl -fsSL -o /opt/UncivServer.jar \
      "https://github.com/yairm210/Unciv/releases/download/${UNCIV_VERSION}/UncivServer.jar"; \
    if [ -n "${UNCIV_SERVER_SHA256}" ]; then \
      echo "${UNCIV_SERVER_SHA256}  /opt/UncivServer.jar" | sha256sum -c -; \
    fi

COPY auth_proxy.py /opt/openhost-unciv/auth_proxy.py
COPY start.sh      /opt/openhost-unciv/start.sh
RUN chmod 0755 /opt/openhost-unciv/start.sh /opt/openhost-unciv/auth_proxy.py

# OpenHost routes the public URL to this port; auth_proxy listens here
# and forwards to UncivServer on loopback :8081.
EXPOSE 8080

CMD ["/opt/openhost-unciv/start.sh"]
