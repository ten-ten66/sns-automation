#!/bin/bash
# SNS Automation - X only auto-posting
# Usage: ./start.sh
#   or:  nohup ./start.sh &

set -e
cd "$(dirname "$0")"

# Load .env into environment (handles parent process overrides)
set -a
source .env
set +a

echo "Starting SNS Automation (X only)..."
echo "  Platform: X"
echo "  API: http://localhost:9000"
echo "  Docs: http://localhost:9000/docs"

exec uvicorn src.api.app:app --host 127.0.0.1 --port 9000
