FROM eclipse-temurin:17-jre-jammy

RUN apt-get update \
    && apt-get install -y --no-install-recommends bash ca-certificates coreutils findutils \
    && rm -rf /var/lib/apt/lists/*

COPY runtime/entrypoint.sh /usr/local/bin/mc-entrypoint
RUN chmod 0755 /usr/local/bin/mc-entrypoint
WORKDIR /server
ENTRYPOINT ["/usr/local/bin/mc-entrypoint"]
