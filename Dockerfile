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

COPY wilman/ wilman/
COPY run.py .

ENV WILMAN_DATA_DIR=/data
VOLUME /data
EXPOSE 8300

# Git identity for wilman's commits (override in compose if you prefer).
RUN git config --system user.name "Wilman" \
    && git config --system user.email "wilman@localhost" \
    && git config --system --add safe.directory '*'

CMD ["python", "run.py"]
