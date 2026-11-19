# 라즈베리파이(arm64)와 x86 모두에서 빌드됨
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /srv
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY app ./app

RUN useradd -r -u 10001 hub && chown -R hub /srv
USER hub
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/healthz')"
# Pi 메모리를 아끼기 위해 워커 1개 (요청량이 적은 팀용)
# --no-access-log: ?token= 이 로그에 남지 않도록
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*", "--no-access-log"]
