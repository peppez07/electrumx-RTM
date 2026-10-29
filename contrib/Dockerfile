# Build from the repository root:
# docker build -f contrib/Dockerfile -t electrumx-rtm .
FROM python:3.10-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential libleveldb-dev ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY . /src
RUN pip install --no-cache-dir /src \
    && rm -rf /src \
    && useradd --system --uid 10001 --create-home electrumx \
    && mkdir /db \
    && chown electrumx:electrumx /db

ENV COIN=Raptoreum \
    NET=mainnet \
    DB_DIRECTORY=/db \
    DB_ENGINE=leveldb \
    SERVICES="tcp://127.0.0.1:50001,rpc://127.0.0.1:8000" \
    CACHE_MB=64 \
    PYTHONUNBUFFERED=1

VOLUME /db
USER electrumx
STOPSIGNAL SIGTERM
CMD ["electrumx_server"]

# Supply DAEMON_URL at runtime. With a Core RPC on the Linux host, use
# --network host. Keep credentials in an external --env-file.
# Bind-mounted /db must be writable by UID 10001. Use a fresh asset DB directory.
