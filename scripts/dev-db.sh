#!/usr/bin/env bash
# Stand up a local TimescaleDB for development and run anduin against it.
#
# nixpkgs' postgresql has no timescaledb extension, and the migrations create
# hypertables + compression policies, so we run the official TimescaleDB image
# in a container rather than a bare local postgres.
#
# Usage:
#   scripts/dev-db.sh up        start the container (if needed) and migrate
#   scripts/dev-db.sh down      stop and remove the container (data is lost)
#   scripts/dev-db.sh reset     down + up (fresh database)
#   scripts/dev-db.sh migrate   run `anduin db migrate` against the container
#   scripts/dev-db.sh psql      open a psql shell in the container
#   scripts/dev-db.sh pull <source> [-- <extra args>]
#                               run a real (writing) extract, e.g.
#                               scripts/dev-db.sh pull withings -- --since 2026-04-01 --until 2026-07-14
#   scripts/dev-db.sh url       print the DATABASE_URL to export
#
# Everything is namespaced so it never collides with a real deployment:
#   container   anduin-tsdb
#   port        127.0.0.1:5432
#   role/db     anduin / anduin   (password: anduin)
set -euo pipefail

CONTAINER=anduin-tsdb
IMAGE="${ANDUIN_TSDB_IMAGE:-docker.io/timescale/timescaledb:2.28.2-pg17}"
PORT="${ANDUIN_TSDB_PORT:-5432}"
DB_URL="postgresql://anduin:anduin@127.0.0.1:${PORT}/anduin"

cd "$(dirname "$0")/.."

# Prefer podman (works rootless; the docker daemon is often not accessible),
# fall back to docker. Override with ANDUIN_CONTAINER_ENGINE.
engine() {
  if [ -n "${ANDUIN_CONTAINER_ENGINE:-}" ]; then echo "$ANDUIN_CONTAINER_ENGINE"; return; fi
  if command -v podman >/dev/null 2>&1; then echo podman; return; fi
  if command -v docker >/dev/null 2>&1; then echo docker; return; fi
  echo "error: no container engine found (need podman or docker)" >&2; exit 1
}
ENGINE="$(engine)"

exists()  { "$ENGINE" container exists "$CONTAINER" 2>/dev/null || "$ENGINE" ps -a --format '{{.Names}}' | grep -qx "$CONTAINER"; }
running() { [ "$("$ENGINE" inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null || echo false)" = true ]; }

# Run an anduin CLI command inside the nix dev shell with the container's URL.
anduin() {
  nix develop -c env PYTHONPATH=src DATABASE_URL="$DB_URL" python -m anduin "$@"
}

wait_ready() {
  # The official image initializes by starting a TEMPORARY postgres with
  # listen_addresses='' (socket only, no TCP) to run init scripts, then restarts
  # it for real. pg_isready would pass against that temp server and we'd hit the
  # restart mid-migrate. So poll over TCP (which the temp server refuses) and
  # require two consecutive successes to be past the bounce.
  echo "waiting for postgres to accept TCP connections..."
  local ok=0
  for _ in $(seq 1 60); do
    if "$ENGINE" exec -e PGPASSWORD=anduin "$CONTAINER" \
        psql -h 127.0.0.1 -U anduin -d anduin -tAc 'SELECT 1' >/dev/null 2>&1; then
      ok=$((ok + 1))
      if [ "$ok" -ge 2 ]; then echo "ready."; return 0; fi
    else
      ok=0
    fi
    sleep 1
  done
  echo "error: postgres did not become ready in 60s" >&2; exit 1
}

start() {
  if running; then echo "$CONTAINER already running."; return; fi
  if exists; then echo "starting existing $CONTAINER..."; "$ENGINE" start "$CONTAINER" >/dev/null;
  else
    echo "creating $CONTAINER from $IMAGE..."
    "$ENGINE" run -d --name "$CONTAINER" \
      -e POSTGRES_USER=anduin -e POSTGRES_PASSWORD=anduin -e POSTGRES_DB=anduin \
      -p "127.0.0.1:${PORT}:5432" \
      "$IMAGE" >/dev/null
  fi
  wait_ready
}

case "${1:-}" in
  up)
    start
    anduin db migrate
    echo
    echo "dev DB ready. To use it in your own commands:"
    echo "  export DATABASE_URL=\"$DB_URL\""
    ;;
  down)
    if exists; then "$ENGINE" rm -f "$CONTAINER" >/dev/null && echo "removed $CONTAINER."; else echo "$CONTAINER not present."; fi
    ;;
  reset)
    "$0" down
    "$0" up
    ;;
  migrate)
    running || { echo "error: $CONTAINER is not running (run: $0 up)" >&2; exit 1; }
    anduin db migrate
    ;;
  psql)
    running || { echo "error: $CONTAINER is not running (run: $0 up)" >&2; exit 1; }
    shift
    exec "$ENGINE" exec -it "$CONTAINER" psql -U anduin -d anduin "$@"
    ;;
  pull)
    running || { echo "error: $CONTAINER is not running (run: $0 up)" >&2; exit 1; }
    shift
    src="${1:?usage: $0 pull <source> [-- <extra args>]}"; shift || true
    [ "${1:-}" = "--" ] && shift || true
    anduin extract "$src" "$@"
    ;;
  url)
    echo "$DB_URL"
    ;;
  *)
    sed -n '2,32p' "$0" | sed 's/^# \{0,1\}//'
    exit 1
    ;;
esac
