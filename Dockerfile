FROM python:3.12-slim

# git for the clones, gh for GitHub, node for the Claude Code runtime the
# Agent SDK drives.
RUN apt-get update && apt-get install -y --no-install-recommends \
        git curl ca-certificates gnupg bash \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

ARG GIT_SHA=""
ENV HARNESS_GIT_SHA=$GIT_SHA

COPY harness/ harness/
COPY run.py .

ENV HARNESS_DATA_DIR=/data
VOLUME /data
EXPOSE 8300

# Git identity for harness's commits (override in compose if you prefer).
RUN git config --system user.name "Harness" \
    && git config --system user.email "harness@localhost" \
    && git config --system --add safe.directory '*'

HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8300/health', timeout=5).status==200 else 1)"]

CMD ["python", "run.py"]
