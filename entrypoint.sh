#!/bin/sh
if [ "$(id -u)" = '0' ]; then
    chown -R appuser:appgroup /data
    exec gosu appuser "$@"
fi
exec "$@"
