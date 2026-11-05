FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 TZ=Asia/Shanghai
WORKDIR /app
COPY agent/ /app/
CMD ["python", "hnpicks.py", "loop"]
