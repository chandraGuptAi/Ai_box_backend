FROM python:3.12-slim-bookworm
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 MPLBACKEND=Agg MPLCONFIGDIR=/tmp/matplotlib
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends libpq5 fonts-dejavu-core && rm -rf /var/lib/apt/lists/* && groupadd --gid 10001 app && useradd --uid 10001 --gid app --create-home app
COPY requirements.txt .
RUN python -m pip install -r requirements.txt && python -m pip check
COPY app ./app
COPY scripts ./scripts
COPY docker ./docker
# Runtime skill registry read by app/report_engine/registry.py (skills/registry.json and SKILL.md files).
COPY skills ./skills
RUN mkdir -p /app/output /app/data && chown -R app:app /app/output /app/data
USER app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 CMD ["python", "docker/healthcheck.py"]
ENTRYPOINT ["python", "/app/docker/start.py"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-proxy-headers"]
