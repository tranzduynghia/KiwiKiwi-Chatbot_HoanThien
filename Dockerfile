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

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl gnupg ca-certificates \
 && curl -fsSL https://pkg.cloudflareclient.com/pubkey.gpg | gpg --yes --dearmor --output /usr/share/keyrings/cloudflare-warp-archive-keyring.gpg \
 && CODENAME="$(. /etc/os-release && echo "${VERSION_CODENAME:-bookworm}")" \
 && echo "deb [signed-by=/usr/share/keyrings/cloudflare-warp-archive-keyring.gpg] https://pkg.cloudflareclient.com/ ${CODENAME} main" > /etc/apt/sources.list.d/cloudflare-client.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends cloudflare-warp \
 && ls -la "$(command -v warp-svc)" "$(command -v warp-cli)" \
 && apt-get purge -y --auto-remove gnupg \
 && rm -rf /var/lib/apt/lists/*

COPY --from=cloudflare/cloudflared:latest /usr/local/bin/cloudflared /usr/local/bin/cloudflared

COPY bot.py init_admin.py docker-entrypoint.sh ./
COPY app/ ./app/
COPY templates/ ./templates/
COPY static/ ./static/

RUN chmod +x /app/docker-entrypoint.sh \
 && useradd --create-home --no-log-init --shell /usr/sbin/nologin botuser \
 && mkdir -p /var/lib/cloudflare-warp /run/cloudflare-warp /var/log/cloudflare-warp /home/botuser/.local/share/warp \
 && chown -R botuser:botuser /var/lib/cloudflare-warp /run/cloudflare-warp /var/log/cloudflare-warp /home/botuser /app \
 && chmod 0775 /var/lib/cloudflare-warp /run/cloudflare-warp /var/log/cloudflare-warp

USER botuser

EXPOSE 10000
ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["python", "bot.py"]
