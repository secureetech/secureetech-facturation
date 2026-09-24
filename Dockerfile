FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

# Railway attribue un port dynamique via $PORT : forme shell pour que la
# variable soit interprétée, avec repli sur 8000 en local.
CMD gunicorn --bind 0.0.0.0:${PORT:-8000} --workers 1 --timeout 60 --access-logfile - --error-logfile - app:app
