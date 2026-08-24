# Dev/single-node database image with pgBackRest baked in — archive_command
# executes inside this container, so the binary must live here.
FROM timescale/timescaledb:2.29.2-pg18
RUN apk add --no-cache pgbackrest \
    && mkdir -p /var/lib/pgbackrest \
    && chown postgres:postgres /var/lib/pgbackrest
