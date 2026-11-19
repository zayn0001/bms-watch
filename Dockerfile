FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 DATA_DIR=/data PORT=10000

# Create data directory for local/mounted state
RUN mkdir -p /data

# Copy project files
COPY . .

EXPOSE 10000
VOLUME /data

# Healthcheck targeting Render health route
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:' + os.getenv('PORT', '10000') + '/api/health')" || exit 1

# mirror.py pulls Supabase state on boot and syncs on shutdown before running server
CMD ["python", "run.py"]
