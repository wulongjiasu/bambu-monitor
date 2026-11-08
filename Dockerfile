FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    BAMBU_PORT=8088

WORKDIR /app

# 优先使用包内离线 wheel，构建时无需访问 PyPI
COPY requirements.txt ./
COPY vendor ./vendor
RUN pip install --no-cache-dir --no-index --find-links /app/vendor -r requirements.txt

COPY server.py VERSION ./
COPY static ./static
COPY tools ./tools
COPY config.example.json ./

EXPOSE 8088

CMD ["sh", "-c", "exec python -u server.py --config /app/config.json --port ${BAMBU_PORT}"]
