FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" \
    PORT=10000

WORKDIR /app

ARG CACHE_BUST=0
ADD https://pypi.org/pypi/google-genai/json /tmp/pypi-google-genai.json
ADD https://pypi.org/pypi/discord.py/json /tmp/pypi-discord.json
ADD https://pypi.org/pypi/pymongo/json /tmp/pypi-pymongo.json

COPY requirements.txt .
RUN echo "cache-bust: ${CACHE_BUST}" \
 && pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir --upgrade --upgrade-strategy eager --force-reinstall -r requirements.txt \
 && (pip list --format=freeze | grep -i -E "^(google-genai|discord.py|pymongo|fastapi|uvicorn|httpx|socksio|aiohttp-socks|pydantic)==" || true) \
 && rm -f /tmp/pypi-*.json

ARG TARGETARCH
ARG WGCF_VERSION=2.3.1
ARG WIREPROXY_VERSION=1.1.3
RUN set -eux \
 && apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && ARCH="${TARGETARCH:-$(dpkg --print-architecture)}" \
 && case "${ARCH}" in \
      amd64|x86_64) ARCH=amd64 ;; \
      arm64|aarch64) ARCH=arm64 ;; \
      *) echo "Unsupported architecture for wgcf/wireproxy: ${ARCH}" >&2; exit 1 ;; \
    esac \
 && curl -fsSL --retry 3 --retry-delay 2 -o /usr/local/bin/wgcf \
      "https://github.com/ViRb3/wgcf/releases/download/v${WGCF_VERSION}/wgcf_${WGCF_VERSION}_linux_${ARCH}" \
 && curl -fsSL --retry 3 --retry-delay 2 \
      "https://github.com/whyvl/wireproxy/releases/download/v${WIREPROXY_VERSION}/wireproxy_linux_${ARCH}.tar.gz" \
    | tar -xz -C /usr/local/bin wireproxy \
 && chmod 0755 /usr/local/bin/wgcf /usr/local/bin/wireproxy \
 && apt-get purge -y --auto-remove curl \
 && rm -rf /var/lib/apt/lists/*

RUN wgcf --help >/dev/null && wireproxy --version

COPY --from=cloudflare/cloudflared:latest /usr/local/bin/cloudflared /usr/local/bin/cloudflared

COPY bot.py init_admin.py docker-entrypoint.sh ./
COPY app/ ./app/
COPY templates/ ./templates/
COPY static/ ./static/

RUN chmod +x /app/docker-entrypoint.sh \
 && useradd --create-home --no-log-init --shell /usr/sbin/nologin botuser \
 && mkdir -p /var/lib/warp-proxy \
 && chown -R botuser:botuser /var/lib/warp-proxy /home/botuser /app \
 && chmod 0775 /var/lib/warp-proxy

USER botuser

EXPOSE 10000
ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["python", "bot.py"]
