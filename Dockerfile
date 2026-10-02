FROM ghcr.io/home-assistant/base-python:3.12-alpine3.24

ARG BUILD_VERSION
ARG BUILD_ARCH

LABEL io.hass.type="app" \
      io.hass.version="${BUILD_VERSION}" \
      io.hass.arch="${BUILD_ARCH}"

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py run.sh ./
RUN chmod a+x /app/run.sh
CMD [ "/app/run.sh" ]
