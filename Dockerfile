FROM python:3.12-slim
WORKDIR /app
COPY server ./server
COPY web ./web
RUN mkdir -p /app/data
EXPOSE 8090/tcp 1900/udp
CMD ["python3", "server/app.py", "-c", "/app/config.json"]
