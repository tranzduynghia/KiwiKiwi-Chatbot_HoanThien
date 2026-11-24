FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
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
 && (pip list --format=freeze | grep -i -E "^(google-genai|discord.py|pymongo|fastapi|uvicorn|httpx|pydantic)==" || true) \
 && rm -f /tmp/pypi-*.json

COPY bot.py config_store.py dashboard.py init_admin.py ./
COPY templates/ ./templates/

RUN useradd --create-home botuser
USER botuser

EXPOSE 10000
CMD ["python", "bot.py"]
