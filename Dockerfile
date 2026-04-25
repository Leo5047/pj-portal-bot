FROM python:3.11-slim

WORKDIR /usr/src/app

ENV PIP_DEFAULT_TIMEOUT=120 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

COPY requirements.txt .
RUN python -m pip install --upgrade pip && \
    pip install --retries 5 -r requirements.txt

COPY pjportal.py .

RUN mkdir -p /data
VOLUME ["/data"]

CMD ["python", "-u", "pjportal.py"]
