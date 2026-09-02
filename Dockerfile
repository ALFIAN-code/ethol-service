FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY ethol-notification.py ./
CMD ["python", "ethol-notification.py"]
