FROM python:3.11-slim

WORKDIR /app
COPY . .
RUN pip install --no-cache-dir -e .

EXPOSE 8000
CMD ["python", "-m", "radiomcp", "--transport", "sse", "--port", "8000"]
