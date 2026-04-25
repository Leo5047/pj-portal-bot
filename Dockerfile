FROM python:3.12-slim

WORKDIR /usr/src/app

# Pure-Python deps - no apt build tools needed
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY pjportal.py .

# Persist cookie + state + raw dumps across container restarts.
# The actual storage backend is provided by the named volume in compose.
RUN mkdir -p /data
VOLUME ["/data"]

CMD ["python", "-u", "pjportal.py"]
