FROM alpine:3.20

RUN addgroup -S app \
    && adduser -S -G app app

WORKDIR /app

USER app

CMD ["sh", "-c", "echo 'replace this with the KOTH service' && sleep infinity"]
