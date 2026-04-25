FROM python:3.12-slim

WORKDIR /usr/src/app

# Install deps first for better layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY pjportal.py .

# Persist cookie + state + raw dumps across container restarts
RUN mkdir -p /data
VOLUME ["/data"]

# No CMD restart needed: the script loops internally
CMD ["python", "-u", "pjportal.py"]
