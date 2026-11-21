FROM python:3.15-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir . && useradd --system --no-create-home --uid 999 app && mkdir /data && chown app /data
USER app
VOLUME /data
ENTRYPOINT ["lombo-visa"]
CMD ["run", "--loop"]
