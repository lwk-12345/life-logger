#!/usr/bin/env bash
# Forward legacy service startup entrypoint to start_kev_service.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$SCRIPT_DIR/start_kev_service.sh" "$@"
