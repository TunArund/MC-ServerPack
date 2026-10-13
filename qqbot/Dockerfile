FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends bash curl ca-certificates netcat-openbsd tzdata util-linux \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir tccli websockets

WORKDIR /app
COPY src/ /app/
COPY entrypoint.sh /usr/local/bin/qqbot-entrypoint
RUN chmod 0755 /usr/local/bin/qqbot-entrypoint /app/*.sh

ENV TCCLI=/usr/local/bin/tccli
ENTRYPOINT ["/usr/local/bin/qqbot-entrypoint"]
