FROM python:3.11-slim
WORKDIR /app
COPY ui/chat /app/chat
COPY agents/templates /app/chat/templates
EXPOSE 3000
CMD ["python", "-m", "http.server", "3000", "--directory", "/app/chat"]

