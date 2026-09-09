FROM alpine:3.23

RUN apk add --no-cache \
    aria2 \
    bash \
    ca-certificates \
    procps \
    py3-yaml \
    python3 \
    wget

WORKDIR /app

COPY server ./server
COPY logo.png ./logo.png
COPY nh2_requireCfToken.sh ./nh2_requireCfToken.sh
RUN chmod 755 /app/nh2_requireCfToken.sh

EXPOSE 8765 8766

ENTRYPOINT ["python3", "/app/server/nh_server.py"]
CMD ["--config", "/app/config.yaml"]
