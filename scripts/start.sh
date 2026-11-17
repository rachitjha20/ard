#!/usr/bin/env sh
# Start ARD on this computer and open it in the browser.
set -e
cd "$(dirname "$0")/.."
exec uv run ard serve --open "$@"
