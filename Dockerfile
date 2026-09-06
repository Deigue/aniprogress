FROM python:3.13-slim

LABEL org.opencontainers.image.title="aniprogress" \
      org.opencontainers.image.description="Syncs anime progress and ratings between Simkl, AniList and MyAnimeList" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 STATE_DIR=/data

WORKDIR /app
COPY aniprogress/ ./aniprogress/

# No third-party dependencies: everything uses the standard library, so there
# is nothing to pin and no supply chain to audit.
RUN useradd -r -u 10001 bridge && mkdir -p /data && chown -R bridge:bridge /app /data
USER bridge
VOLUME ["/data"]

HEALTHCHECK --interval=5m --timeout=10s --start-period=30s \
  CMD python -c "import os,sys,time; p=os.path.join(os.environ.get('STATE_DIR','/data'),'state.json'); sys.exit(0)"

ENTRYPOINT ["python", "-m", "aniprogress.main"]
