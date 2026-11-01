# One-step Swagger UI:  docker build -t troop .  &&  docker run --rm -p 8077:8077 troop
# then open http://127.0.0.1:8077/docs
# Run the CLI instead:  docker run --rm troop troop list
FROM python:3.12-slim

WORKDIR /app
COPY . .
RUN pip install --no-cache-dir ".[all]"

# serve on all interfaces so the port is reachable from the host
ENV TROOP_WEB_HOST=0.0.0.0 \
    TROOP_WEB_PORT=8077 \
    TROOP_CONFIG=/app/chaos.yaml
EXPOSE 8077

# default command is the Swagger UI; override with `... troop <cmd>` for the CLI
CMD ["troop-web"]
