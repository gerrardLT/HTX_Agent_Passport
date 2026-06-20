#!/bin/sh
set -e

echo "⏳ Running database migrations..."
if alembic upgrade head 2>/dev/null; then
    echo "✅ Migrations complete"
else
    echo "⚠️ Migrations skipped (alembic not configured or DB not reachable)"
fi

echo "🚀 Starting HTX Agent Passport API..."
exec "$@"
