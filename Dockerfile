# Single container running both the Acme sandbox (internal, :8000) and the AI Worker UI (public, :7860).
# Works on Hugging Face Spaces (Docker SDK, free CPU tier) or any Docker host.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/playwright \
    WORKER_RUNS_DIR=/tmp/runs \
    PORT=7860

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
 && python -m playwright install --with-deps chromium \
 && chmod -R a+rx /opt/playwright

COPY . .
RUN chmod +x start.sh

# Hugging Face Spaces runs containers as a non-root user (uid 1000).
RUN useradd -m -u 1000 app && chown -R app /app
USER app

EXPOSE 7860
CMD ["./start.sh"]
