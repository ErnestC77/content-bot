#!/usr/bin/env sh
set -e

echo "Применяю миграции базы данных…"
alembic upgrade head

echo "Запускаю приложение…"
exec uvicorn app.main:app --host "${APP_HOST:-0.0.0.0}" --port "${APP_PORT:-8000}"
