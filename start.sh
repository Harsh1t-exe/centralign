#!/bin/sh
# Container entrypoint: the sandbox stays private inside the container; only the worker UI is exposed.
set -e
python -m uvicorn acme.app:app --host 127.0.0.1 --port 8000 --log-level warning &
exec python -m worker serve --host 0.0.0.0 --port "${PORT:-7860}"
