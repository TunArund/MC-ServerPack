FROM eclipse-temurin:17-jre-jammy

ARG APT_MIRROR="mirrors.aliyun.com"
RUN if [ -n "$APT_MIRROR" ]; then \
        sed -i "s|archive.ubuntu.com|${APT_MIRROR}|g; s|security.ubuntu.com|${APT_MIRROR}|g" /etc/apt/sources.list; \
    fi \
    && apt-get update \
    && apt-get install -y --no-install-recommends bash ca-certificates coreutils findutils unzip \
    && rm -rf /var/lib/apt/lists/*

COPY runtime/entrypoint.sh /usr/local/bin/mc-entrypoint
RUN chmod 0755 /usr/local/bin/mc-entrypoint
# Runtime UID/GID come from the generated compose file so Minecraft runs as the
# user executing manager.sh, never root. Do not bake a fixed USER here.
WORKDIR /server
ENTRYPOINT ["/usr/local/bin/mc-entrypoint"]
