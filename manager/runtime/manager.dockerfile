FROM docker:28-cli

ARG APK_MIRROR="mirrors.aliyun.com"
ARG PIP_INDEX_URL="https://mirrors.aliyun.com/pypi/simple/"
RUN if [ -n "$APK_MIRROR" ]; then \
        sed -i "s|dl-cdn.alpinelinux.org|${APK_MIRROR}|g" /etc/apk/repositories; \
    fi \
    && apk add --no-cache python3 py3-pip tzdata openssl \
    && python3 -m venv /opt/manager \
    && if [ -n "$PIP_INDEX_URL" ]; then \
        /opt/manager/bin/pip install --no-cache-dir --index-url "$PIP_INDEX_URL" --upgrade pip; \
    else \
        /opt/manager/bin/pip install --no-cache-dir --upgrade pip; \
    fi

ENV PATH=/opt/manager/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY src/requirements.txt ./src/requirements.txt
RUN if [ -n "$PIP_INDEX_URL" ]; then \
        pip install --no-cache-dir --index-url "$PIP_INDEX_URL" -r src/requirements.txt; \
    else \
        pip install --no-cache-dir -r src/requirements.txt; \
    fi
COPY src ./src

CMD ["python", "-m", "src.app"]
