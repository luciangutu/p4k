#!/usr/bin/env bash
# Start a throwaway p4d in Docker and populate it with sample history
# (adds, edits, deletes, moves, integrations, binary files, multiple users,
# and enough changelists to exercise paging in p4k).
#
# Usage:   dev/seed.sh            # build + start container, seed data
# Then:    source dev/env.sh && .venv/bin/python -m p4k
# Cleanup: docker rm -f p4k-p4d
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
CONTAINER=p4k-p4d
HOST_PORT=1777

# shellcheck source=env.sh
source "$HERE/env.sh"

P4D_URL=https://ftp.perforce.com/perforce/r25.2/bin.linux26x86_64/p4d
if [[ ! -f "$HERE/p4d/p4d" ]]; then
    echo "Downloading p4d ..."
    curl -sSfL --retry 3 -o "$HERE/p4d/p4d" "$P4D_URL"
fi

docker build -q -t p4k-p4d "$HERE/p4d" >/dev/null
docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
docker run -d --name "$CONTAINER" -p "127.0.0.1:${HOST_PORT}:1666" p4k-p4d >/dev/null

echo "Waiting for p4d on $P4PORT ..."
for _ in $(seq 1 30); do
    p4 info >/dev/null 2>&1 && break
    sleep 0.5
done
p4 info >/dev/null

WS="$(mktemp -d)"
trap 'rm -rf "$WS"' EXIT
cd "$WS"

# Create one workspace per user, all sharing the same root directory.
make_client() {
    local user=$1
    p4 -u "$user" -c "ws-$user" client -o \
        | sed "s#^Root:.*#Root: $WS#" \
        | p4 -u "$user" -c "ws-$user" client -i >/dev/null
    p4 -u "$user" -c "ws-$user" sync -q >/dev/null 2>&1 || true
}

# Run a p4 command as a given user in that user's workspace.
as() { local user=$1; shift; p4 -u "$user" -c "ws-$user" "$@"; }

submit() { local user=$1; shift; as "$user" submit -d "$*" >/dev/null; }

for u in alice bob carol; do make_client "$u"; done

# --- Initial import -------------------------------------------------------
mkdir -p main/src main/docs main/assets
for i in 1 2 3; do
    printf 'int func%s(void)\n{\n    return %s;\n}\n' "$i" "$i" > "main/src/module$i.c"
done
printf '# Project\n\nSample project used to test p4k.\n' > main/docs/README.md
head -c 2048 /dev/urandom > main/assets/logo.png
as alice add -t binary main/assets/logo.png >/dev/null
as alice add main/src/*.c main/docs/README.md >/dev/null
submit alice "Initial import of the sample project"

# --- Edit with a multi-line description -----------------------------------
as bob sync -q >/dev/null
as bob edit main/src/module1.c >/dev/null
cat > main/src/module1.c <<'EOF'
#include <stdio.h>

int func1(void)
{
    printf("func1 called\n");
    return 1;
}
EOF
as bob submit -d "Add logging to func1

This change adds a printf so we can trace calls.
Reviewed-by: carol" >/dev/null

# --- Delete + move in the same CL -----------------------------------------
as carol sync -q >/dev/null
as carol delete main/src/module3.c >/dev/null
as carol edit main/src/module2.c >/dev/null
as carol move main/src/module2.c main/src/core.c >/dev/null
submit carol "Remove module3, rename module2 to core"

# --- Branch (integrate) ----------------------------------------------------
as alice sync -q >/dev/null
as alice integrate -q //depot/main/... //depot/release/... >/dev/null
submit alice "Branch main to release"

# --- Binary edit -----------------------------------------------------------
as bob sync -q >/dev/null
as bob edit main/assets/logo.png >/dev/null
head -c 4096 /dev/urandom > main/assets/logo.png
submit bob "Update logo"

# --- Many small edits to exercise paging -----------------------------------
users=(alice bob carol)
for n in $(seq 1 240); do
    u=${users[$((n % 3))]}
    as "$u" sync -q >/dev/null
    as "$u" edit main/docs/README.md >/dev/null
    echo "Line $n added by $u" >> main/docs/README.md
    submit "$u" "Docs update #$n by $u"
done

echo "Seeded $(p4 changes -s submitted | wc -l) changelists on $P4PORT"
