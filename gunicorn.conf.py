# Configuración automática de Gunicorn para soportar peticiones concurrentes
# (evita bloqueos cuando Google Wallet descarga strip.png durante POST/PATCH loyaltyObject)
workers = 1
threads = 16
timeout = 120
