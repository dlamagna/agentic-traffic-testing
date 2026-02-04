FROM python:3.11-slim
WORKDIR /app

# Copy all UI directories
COPY ui/chat /app/chat
COPY ui/agentverse /app/agentverse

# Copy templates to both locations for access
COPY agents/templates /app/chat/templates
COPY agents/templates /app/agentverse/templates

# Create a simple index page to navigate between UIs
RUN echo '<!DOCTYPE html>\n\
<html lang="en">\n\
<head>\n\
  <meta charset="UTF-8">\n\
  <meta name="viewport" content="width=device-width, initial-scale=1.0">\n\
  <title>Agent Testbed</title>\n\
  <style>\n\
    body { font-family: -apple-system, BlinkMacSystemFont, sans-serif; margin: 0; min-height: 100vh; display: flex; align-items: center; justify-content: center; background: linear-gradient(135deg, #0f172a 0%, #1e293b 100%); color: #f1f5f9; }\n\
    .container { text-align: center; padding: 40px; }\n\
    h1 { font-size: 36px; margin-bottom: 12px; }\n\
    p { color: #94a3b8; margin-bottom: 40px; }\n\
    .cards { display: flex; gap: 24px; justify-content: center; flex-wrap: wrap; }\n\
    .card { background: #1e293b; border: 1px solid #334155; border-radius: 16px; padding: 32px; width: 280px; text-decoration: none; color: inherit; transition: all 0.3s; }\n\
    .card:hover { border-color: #3b82f6; transform: translateY(-4px); box-shadow: 0 12px 40px rgba(59, 130, 246, 0.15); }\n\
    .card h2 { margin: 0 0 8px; font-size: 20px; }\n\
    .card p { margin: 0; font-size: 14px; color: #94a3b8; }\n\
    .icon { font-size: 48px; margin-bottom: 16px; }\n\
  </style>\n\
</head>\n\
<body>\n\
  <div class="container">\n\
    <h1>Agent Testbed</h1>\n\
    <p>Multi-agent collaboration and traffic analysis platform</p>\n\
    <div class="cards">\n\
      <a href="/chat/" class="card">\n\
        <div class="icon">💬</div>\n\
        <h2>Classic Chat</h2>\n\
        <p>Simple agent chat interface with parallel and multi-hop scenarios</p>\n\
      </a>\n\
      <a href="/agentverse/" class="card">\n\
        <div class="icon">🌐</div>\n\
        <h2>AgentVerse</h2>\n\
        <p>4-stage workflow with dynamic expert recruitment and collaborative decision-making</p>\n\
      </a>\n\
    </div>\n\
  </div>\n\
</body>\n\
</html>' > /app/index.html

EXPOSE 3000
CMD ["python", "-m", "http.server", "3000", "--directory", "/app"]

