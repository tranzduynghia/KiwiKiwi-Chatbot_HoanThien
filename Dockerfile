FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=10000

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .

# chạy bằng user thường, không dùng root
RUN useradd --create-home botuser
USER botuser

EXPOSE 10000
CMD ["python", "bot.py"]
