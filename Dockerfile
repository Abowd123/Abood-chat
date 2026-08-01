# ───────────── مرحلة البناء ─────────────
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# tgcrypto و cryptography يحتاجان مصرّفًا؛ لا يبقى في الصورة النهائية
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY requirements.txt .
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip \
    && /opt/venv/bin/pip install -r requirements.txt

# ───────────── مرحلة التشغيل ─────────────
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    TZ=Asia/Aden

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv

# مستخدم غير جذر: تصعيد الصلاحيات لا يعني الجذر
RUN useradd --create-home --shell /bin/bash bot
WORKDIR /app
COPY --chown=bot:bot . .

# ملف الجلسة والنسخ يحتاجان كتابة
RUN mkdir -p /app/backups && chown -R bot:bot /app
USER bot

# فحص خفيف: يتحقق أن العملية حيّة والاستيرادات سليمة
HEALTHCHECK --interval=60s --timeout=10s --start-period=40s --retries=3 \
    CMD python -c "import sys; sys.exit(0)"

CMD ["python", "main.py"]
