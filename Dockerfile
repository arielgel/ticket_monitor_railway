# Imagen oficial de Playwright con navegadores y deps ya instaladas
FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy

# Evita buffer en logs
ENV PYTHONUNBUFFERED=1

# Crea carpeta de app
WORKDIR /app

# Solo necesitamos requests; playwright ya viene en la imagen
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copia el monitor
COPY monitor_renderizado.py /app/monitor_renderizado.py

# Variables por defecto (las reales van como secrets en Railway)
ENV URL="https://www.allaccess.com.ar/event/airbag"
ENV CHECK_EVERY_SECONDS="300"  # 5 min por defecto

# Comando de arranque
CMD ["python", "/app/monitor_renderizado.py"]
