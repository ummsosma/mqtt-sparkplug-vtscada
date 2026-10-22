# Collector and bridge. Both run the same image with a different entry point.
#
# There is no curl in here on purpose: the VTScada REST client uses urllib, so
# the image needs nothing beyond the Python dependencies.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt pyproject.toml ./
COPY src/ ./src/

# Install the package so `python -m pipeline.collector` works from anywhere.
RUN pip install --no-cache-dir -r requirements.txt && \
    pip install --no-cache-dir --no-deps .

COPY tools/ ./tools/
COPY tests/ ./tests/

# The bridge persists its bdSeq counter here. Losing it on every restart makes
# VTScada treat the next BIRTH as stale, after which it silently ignores every
# DATA message -- so mount a volume at this path.
RUN mkdir -p /var/lib/pipeline && \
    useradd --create-home --uid 10001 pipeline && \
    chown -R pipeline:pipeline /var/lib/pipeline
VOLUME ["/var/lib/pipeline"]

USER pipeline

# Overridden per service in docker-compose.yml.
CMD ["python", "-m", "pipeline.collector"]
