# Environment for talking to the local development p4d started by dev/seed.sh.
# Usage: source dev/env.sh
export P4PORT=localhost:1777
export P4USER=alice
export P4CLIENT=ws-alice
export P4CHARSET=none
# Ignore any P4CONFIG / P4ENVIRO settings from the real environment.
export P4CONFIG=
export P4ENVIRO=/dev/null
