FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install --with-deps chromium

COPY app.py manage.py ./
COPY spark/ spark/
COPY douyin/ douyin/
COPY web/ web/

ENV DATA_DIR=/app/data \
    HOST=0.0.0.0 \
    PORT=8020 \
    TZ=Asia/Shanghai

VOLUME ["/app/data"]
EXPOSE 8020

CMD ["python", "app.py"]
