FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV DEMO_CONFIGS=/app/demo_bots
CMD ["python", "main.py"]
