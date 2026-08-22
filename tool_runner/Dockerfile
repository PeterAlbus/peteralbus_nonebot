FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends bash coreutils curl git jq \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace
USER 65534:65534
ENTRYPOINT ["/bin/bash", "-lc"]
