# Throwaway Helix Core (p4d) server used only for local development/testing
# of p4k. Not meant for production use.
#
# The p4d binary is downloaded on the host by dev/seed.sh (more reliable than
# fetching it from inside the Docker build) and copied into the image.
FROM debian:bookworm-slim

COPY p4d /usr/local/bin/p4d
RUN chmod +x /usr/local/bin/p4d && mkdir -p /p4root

EXPOSE 1666
# -r: server root, -p: listen port, -L: log file.
# The free-space minimums are disabled so the server starts on small disks.
CMD p4d -r /p4root "-cset filesys.P4ROOT.min=0" \
 && p4d -r /p4root "-cset filesys.P4LOG.min=0" \
 && p4d -r /p4root "-cset filesys.P4JOURNAL.min=0" \
 && p4d -r /p4root "-cset filesys.depot.min=0" \
 && exec p4d -r /p4root -p 1666 -L /p4root/log
