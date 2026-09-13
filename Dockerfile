FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# 国内网络：apt 源切到清华 TUNA 镜像（deb.debian.org 直连不稳）
RUN sed -i 's@deb.debian.org@mirrors.tuna.tsinghua.edu.cn@g' /etc/apt/sources.list.d/debian.sources 2>/dev/null; \
    sed -i 's@deb.debian.org@mirrors.tuna.tsinghua.edu.cn@g' /etc/apt/sources.list 2>/dev/null; \
    true
# 容器内仅无头运行（可视窗口由代码自动回退），--only-shell 依赖更少、体积更小
RUN playwright install --with-deps --only-shell chromium

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
