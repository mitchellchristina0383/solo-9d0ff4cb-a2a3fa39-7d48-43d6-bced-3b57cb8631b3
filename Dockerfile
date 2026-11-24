FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY common /app/common
COPY gateway /app/gateway
COPY publisher /app/publisher
COPY samples /app/samples

RUN groupadd --gid 10001 gateway \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin gateway

USER 10001:10001

EXPOSE 8000 8001 9000

CMD ["gunicorn", "--workers", "1", "--bind", "0.0.0.0:8000", "gateway.app:app"]
