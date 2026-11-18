FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PORT=8080
RUN useradd -r -u 10001 app
WORKDIR /app
COPY application/app.py /app/app.py
COPY application/index.html /app/index.html
USER 10001
EXPOSE 8080
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=2)"
CMD ["python", "/app/app.py"]
