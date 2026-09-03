#!/bin/sh
set -e

cd /app

echo ">> Starting Uvicorn, Celery, and Nginx via Supervisor..."
exec /usr/bin/supervisord -c /etc/supervisor/conf.d/supervisord.conf