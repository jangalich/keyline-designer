FROM python:3.11-slim

# WeasyPrint needs Pango, Cairo, and GDK-Pixbuf (and their dependencies)
# at the system level -- pip can't provide these, and Render's native
# Python runtime doesn't include them either, so this project deploys
# via Docker instead of Render's native runtime.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    libpangoft2-1.0-0 \
    libcairo2 \
    libgdk-pixbuf2.0-0 \
    libffi-dev \
    shared-mime-info \
    fonts-liberation \
    fontconfig \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD gunicorn api:app --bind 0.0.0.0:$PORT
