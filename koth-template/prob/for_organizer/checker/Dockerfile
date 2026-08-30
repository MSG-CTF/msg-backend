FROM python:3.12-slim

RUN addgroup --system checker \
    && adduser --system --ingroup checker --home /checker checker

WORKDIR /checker
COPY --chown=checker:checker checker.py .

USER checker

CMD ["python", "checker.py"]
