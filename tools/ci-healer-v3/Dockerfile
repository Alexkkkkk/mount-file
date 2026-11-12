FROM python:3.12-slim

RUN useradd -m -u 1000 sandbox
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace
COPY tools/ci-healer-v3/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

USER sandbox
