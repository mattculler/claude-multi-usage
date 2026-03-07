FROM python:3.12-slim

WORKDIR /app

COPY . .
RUN pip install --no-cache-dir .[server]

VOLUME /data
EXPOSE 8000

CMD ["cmu", "server", "start", "--host", "0.0.0.0", "--port", "8000"]
