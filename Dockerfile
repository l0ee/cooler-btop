FROM python:3.15-slim@sha256:94d3426f1def7db4a106a232ac121bfd975393e5dfd40a619b955fae45fe7913

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY . .
RUN python -m pip install --no-cache-dir -r requirements.lock \
    && python -m pip install --no-cache-dir --no-deps . \
    && addgroup --system cooler-btop \
    && adduser --system --ingroup cooler-btop --no-create-home cooler-btop

USER cooler-btop

EXPOSE 8080

# The daemon requires an explicit credential for the container's non-loopback
# bind. Pass it at runtime with -e rather than baking a secret into the image.
CMD ["sh", "-c", "test -n \"${COOLER_BTOP_AUTH_TOKEN:?COOLER_BTOP_AUTH_TOKEN must be set}\" && exec python -m cooler_btop --daemon --host 0.0.0.0 --port 8080"]
