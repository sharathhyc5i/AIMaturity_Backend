#!/bin/bash
set -e
echo "Starting FastAPI Retail AI Readiness API on Azure App Service..."
PORT="${PORT:-8000}"
export PYTHONPATH="${PYTHONPATH}:$(pwd):$(pwd)/backend"

if [ -d "backend" ]; then
    exec gunicorn --bind="0.0.0.0:${PORT}" --workers=2 --worker-class=uvicorn.workers.UvicornWorker --timeout=600 backend.app.main:app
else
    exec gunicorn --bind="0.0.0.0:${PORT}" --workers=2 --worker-class=uvicorn.workers.UvicornWorker --timeout=600 app.main:app
fi
