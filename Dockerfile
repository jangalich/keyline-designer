FROM python:3.11-slim-bookworm

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


# The full report pipeline (climate/soil/elevation/hydrology/imagery
# fetches, LLM narrative generation, and -- for the PDF endpoint --
# DEM fetch + map rendering + PDF assembly) genuinely runs well past
# gunicorn's 30s default worker timeout, which kills in-flight
# requests and returns a generic 500 instead of letting them finish.
CMD gunicorn api:app --bind 0.0.0.0:$PORT --workers 1 --timeout 600
