# Stamp the build with the commit it came from, so the footer names the code
# that is actually running. CI passes GIT_SHA in; a plain `docker build` reads
# the build context's .git here instead. Done in a throwaway stage: no git
# history ends up in the shipped image, and no .git means an empty stamp
# (the footer then says "unknown build") rather than a failed build.
FROM python:3.12-slim AS gitstamp
WORKDIR /gitctx
COPY .git* ./
RUN sha=""; \
    if [ -f HEAD ]; then \
        ref=$(cut -d' ' -f2 HEAD); \
        case "$(cat HEAD)" in \
            ref:*) sha=$(cat "$ref" 2>/dev/null || awk -v r="$ref" '$2==r {print $1}' packed-refs 2>/dev/null || true);; \
            *) sha=$(cat HEAD);; \
        esac; \
    fi; \
    printf '%s' "$sha" | cut -c1-7 > /build_sha

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

# Headless Chromium for harness/render.py: without it the engineers write
# CSS and "verify" it by reading the diff. Installed to a path outside any
# HOME, because agent sessions and project commands run with a scratch one.
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN python -m playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

# Set by CI; overrides the stamp below when present.
ARG GIT_SHA=""
ENV HARNESS_GIT_SHA=$GIT_SHA

COPY harness/ harness/
COPY run.py .
COPY --from=gitstamp /build_sha harness/_build_sha

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
