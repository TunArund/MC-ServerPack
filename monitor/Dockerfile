FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY src/requirements.txt ./src/requirements.txt
RUN pip install --no-cache-dir -r src/requirements.txt
COPY src ./src

CMD ["python", "-m", "src.app"]
