FROM python:3.10-slim

WORKDIR /app

# सिस्टम डिपेंडेंसीज़ और Playwright Chromium इंस्टॉल करें
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt
RUN pip install chromium --with-deps
RUN pip install playwright

COPY . .

CMD ["gunicorn", "app:app", "--bind", "0.0.0.0:10000"]
