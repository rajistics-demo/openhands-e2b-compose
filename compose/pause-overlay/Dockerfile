FROM ghcr.io/openhands/enterprise-server:1.88.0@sha256:cc67d10163e9e466b65d7a6e765a020fcc23ca036e78c6a6d846c458d1e2d9eb
USER root
COPY e2b-pause.patch /tmp/e2b-pause.patch
RUN cd /app && patch --batch --forward -p1 -i /tmp/e2b-pause.patch \
    && rm /tmp/e2b-pause.patch
USER openhands
