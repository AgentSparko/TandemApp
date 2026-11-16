# TandemApp, the controller with its page, gateway and chat, as an image: no engine, no GPU, no torch
# (OPS-282: TandemApp in one container). The build context is this tree's root
# (packaging/Dockerfile.app.dockerignore says what goes in):
#
#   docker build -f packaging/Dockerfile.app -t tandemllm-app .
#
# Run it with the LAN in view, so Add engine finds the engines that advertise themselves (mDNS), on
# loopback only:
#   docker run -d --name tandem-app --network host -v tandem-app:/data \
#       -e TANDEM_HOST=127.0.0.1 tandemllm-app
# or behind a published port (then type the engine's address in Add engine):
#   docker run -d --name tandem-app -p 127.0.0.1:8080:8080 -v tandem-app:/data tandemllm-app
#
# Everything the controller keeps lives in the /data volume: the config database, the usage
# ledger, the fleet, the master key (secrets.key, made at the first start unless
# TANDEM_SECRETS_KEY is given), the engine hosts' SSH keys and pins, chatd's env file and chats. The
# OpenSSH client is inside: engine hosts are controlled over SSH with keys TandemApp keeps there.
# The server binds 0.0.0.0 inside the container; what is reachable is what `-p` publishes, or with
# --network host every address of the machine: then set TANDEM_HOST=127.0.0.1 or an admin password
# (TANDEM_ADMIN_TOKEN). With no admin password every route is open (docs/proxy.md). With
# -e TANDEM_AUTH=accounts nothing is open: the first log lines hold a one-time setup code that
# makes the owner (`docker logs`), and `tandem-app users setup-code` prints another.
#
# A new container runs bare (OPS-288): the gateway in front of the engines it pairs, Chat and the
# conductor off until an admin switches them on in Settings, App. Chat then runs in the container
# (packaging/container-start.sh records the choice in /data/container.env; a /data an older image
# made keeps chat on). Its sandboxes need a container runtime's socket at /run/tandem/sandbox.sock
# (chatd.env says where), else chat runs without its sandbox tools and the health page says so:
# for example `-v /var/run/docker.sock:/run/tandem/sandbox.sock --group-add <the socket's group
# id>`, or a rootless Podman socket with `--user` of its owner.
#
# The System view's host collector sees the container, not the machine. For the machine and the
# processes behind backends on it, run the container with `--pid=host` (and `--gpus all` for
# NVML), or run `tandem-app hostcollect` on that machine and point the backend's host at it.
#
# The page is built here, in a Node stage (OPS-246: dashboard/dist is not in git): `npm ci` with the
# lockfile, then `npm run build` (type check and Vite). The wheel stage records its manifest
# (dashboard/dist/bundle.json, the bundle's content hash) and the build backend checks it.

FROM node:22.12.0-bookworm-slim AS page
WORKDIR /src/dashboard
COPY dashboard/package.json dashboard/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY dashboard ./
RUN npm run build

FROM python:3.12-slim AS build
WORKDIR /src
RUN pip install --no-cache-dir build==1.3.0
COPY pyproject.toml VERSION MANIFEST.in LICENSE COMMERCIAL-LICENSE.md NOTICE ./
COPY docs/proxy.md docs/proxy.md
COPY proxy proxy
COPY chatd chatd
COPY ops/chatd.sh ops/chatd.sh
COPY tandemapp tandemapp
COPY packages packages
COPY packaging/tandemapp_build.py packaging/tandemapp_build.py
COPY tools/dashboard_bundle.py tools/dashboard_bundle.py
COPY --from=page /src/dashboard/package-lock.json dashboard/package-lock.json
COPY --from=page /src/dashboard/dist dashboard/dist
RUN python tools/dashboard_bundle.py record --source docker \
    && python tools/dashboard_bundle.py verify \
    && python -m build --wheel -o /wheel .

FROM python:3.12-slim
# the OpenSSH client: engine hosts are controlled over SSH (proxy/enginectl.py, ssh-keyscan too)
RUN apt-get update \
    && apt-get install -y --no-install-recommends openssh-client \
    && apt-get clean
RUN useradd --create-home --uid 10001 tandem && mkdir -p /data /run/tandem && chown tandem /data
COPY --from=build /wheel/*.whl /tmp/
RUN pip install --no-cache-dir "$(ls /tmp/*.whl)[host]" && rm /tmp/*.whl
COPY packaging/container-start.sh /usr/local/lib/tandemapp/container-start.sh
USER tandem
ENV TANDEM_CONFIG_DB=/data/config.sqlite3 \
    TANDEM_USAGE_LEDGER=/data/ledger.sqlite3 \
    TANDEM_APP_STATE=/data \
    TANDEM_HOST=0.0.0.0 \
    TANDEM_PORT=8080 \
    TANDEM_CONDUCTOR=off \
    TANDEM_NODE_AGENT=off \
    TANDEM_DEPLOY=container \
    PYTHONUNBUFFERED=1
VOLUME ["/data"]
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import os, urllib.request as u; u.urlopen('http://127.0.0.1:%s/health' % os.environ.get('TANDEM_PORT', '8080'), timeout=4)"
STOPSIGNAL SIGTERM
ENTRYPOINT ["/bin/sh", "/usr/local/lib/tandemapp/container-start.sh"]
CMD ["serve", "--no-keychain"]
