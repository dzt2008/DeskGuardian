FROM python:3.10-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DESKGUARDIAN_DATA_DIR=/data

WORKDIR /app

COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt \
    && groupadd --system deskguardian \
    && useradd --system --gid deskguardian --home-dir /nonexistent --shell /usr/sbin/nologin deskguardian \
    && mkdir -p /data \
    && chown deskguardian:deskguardian /data

COPY --chown=deskguardian:deskguardian . .

USER deskguardian:deskguardian

EXPOSE 8080
VOLUME ["/data"]

# Binding to all container interfaces is required for Docker port publishing.
# Publish only to the host loopback interface, for example:
#   docker run --rm -p 127.0.0.1:8080:8080 deskguardian
# DeskGuardian has no remote-access authentication and must not be exposed to an
# untrusted network. The scheduler is disabled because containers normally lack
# access to the host desktop notification service.
CMD ["python", "main.py", "web", "--host", "0.0.0.0", "--no-browser", "--no-scheduler"]
