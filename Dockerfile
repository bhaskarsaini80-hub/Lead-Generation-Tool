# Microsoft की ऑफिशियल Playwright इमेज (इसमें ब्राउज़र पहले से इंस्टॉल रहता है)
FROM mcr.microsoft.com/playwright/python:v1.48.0-noble

WORKDIR /app

# ब्राउज़र का पाथ फिक्स करें ताकि Render को ब्राउज़र तुरंत मिल जाए
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

COPY requirements.txt .

# पैकेज और क्रोमियम इंस्टॉल करें
RUN pip install --no-cache-dir -r requirements.txt
RUN playwright install chromium

COPY . .

# ऐप को रन करें
CMD ["gunicorn", "app:app", "--bind", "0.0.0.0:10000", "--workers", "1", "--threads", "4", "--timeout", "120"]
