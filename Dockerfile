FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt ./requirements.txt
COPY vneurok ./vneurok
COPY web ./web
RUN useradd --uid 10001 --create-home app && mkdir /data && chown app:app /data
USER app
EXPOSE 8765
VOLUME ["/data"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 CMD ["python", "-m", "vneurok.healthcheck"]
# A MAX-mode pilot only: no public demo identities in a container.
CMD ["python", "-m", "vneurok.server", "--max-mode", "--send-enabled", "--host", "0.0.0.0", "--db", "/data/vneurok.db"]
