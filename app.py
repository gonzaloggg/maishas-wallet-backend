from flask import Flask, request, jsonify, send_file, render_template_string, redirect, url_for, Response
from werkzeug.middleware.proxy_fix import ProxyFix
from functools import wraps
from datetime import datetime, timezone
from email.utils import formatdate
import os, json, hashlib, zipfile, subprocess, uuid, httpx, ssl, sqlite3, tempfile, shutil

app = Flask(__name__)
# Respetar los encabezados del túnel / proxy HTTPS (X-Forwarded-Proto / Host)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

def check_auth(username, password):
    return username == 'maishas' and password == os.environ.get('BARISTA_PASSWORD', 'cafe2026')

def authenticate():
    return Response(
        'Acceso Denegado. Ingresa las credenciales del panel.',
        401,
        {'WWW-Authenticate': 'Basic realm="Login Panel Barista"'}
    )

def requires_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        if not auth or not check_auth(auth.username, auth.password):
            return authenticate()
        return f(*args, **kwargs)
    return decorated


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PASS_TEMPLATE_DIR = os.path.join(BASE_DIR, "maishas-cafe-pass")

# DATA_DIR será el volumen persistente en Railway. En local usa la carpeta "data"
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(BASE_DIR, "data"))
os.makedirs(DATA_DIR, exist_ok=True)

KEY_PEM = os.path.join(DATA_DIR, "key.pem")
CERT_PEM = os.path.join(DATA_DIR, "cert.pem")
WWDR_PEM = os.path.join(DATA_DIR, "wwdr.pem")
DB_PATH = os.path.join(DATA_DIR, "loyalty.db")


# Auto-inyectar certificados desde Variables de Entorno en Railway
for env_var, file_path in [("ENV_KEY_PEM", KEY_PEM), ("ENV_CERT_PEM", CERT_PEM), ("ENV_WWDR_PEM", WWDR_PEM)]:
    val = os.environ.get(env_var)
    if val:
        val = val.replace('\\n', '\n').strip() + '\n'
        with open(file_path, 'w') as f:
            f.write(val)

AUTH_TOKEN = os.environ.get("AUTH_TOKEN", "MaishasSecretAuthToken2026")
PASS_TYPE_ID = "pass.com.maishascafe.loyalty"
DEFAULT_MESSAGE = "¡Bienvenido a Maishas Café! Aquí verás nuestras promociones."
DEFAULT_LAT = 24.8049
DEFAULT_LNG = -107.3940
DEFAULT_GEO_TEXT = "☕️ ¡Estás cerca de Maishas Café! Pasa por tu café favorito y suma tu sello de hoy 🎃"


def get_utc_timestamp():
    """Retorna timestamp UTC con precisión de microsegundos compatible con SQLite."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")


def get_short_time():
    """Retorna hora legible UTC para mostrar en el panel."""
    return datetime.now(timezone.utc).strftime("%H:%M:%S UTC")


def get_vip_tier(total_visits):
    """Calcula el nivel VIP del cliente según su histórico acumulado de visitas."""
    visits = total_visits or 0
    if visits >= 25:
        return {
            "name": "Socio Oro VIP 👑",
            "badge": "👑 Oro VIP",
            "bg": "#d4af37",
            "fg": "#1C1613",
            "next_goal": "¡Máximo nivel alcanzado!"
        }
    elif visits >= 10:
        return {
            "name": "Socio Plata 🥈",
            "badge": "🥈 Plata",
            "bg": "#bdc3c7",
            "fg": "#1C1613",
            "next_goal": f"A {25 - visits} visitas de Oro VIP 👑"
        }
    else:
        return {
            "name": "Amante del Café ☕️",
            "badge": "🌱 Bronce",
            "bg": "#6E5036",
            "fg": "#F3EDE2",
            "next_goal": f"A {10 - visits} visitas de Nivel Plata 🥈"
        }


def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS clients (
            serial TEXT PRIMARY KEY,
            name TEXT,
            email TEXT,
            phone TEXT,
            birthdate TEXT,
            stamps INTEGER DEFAULT 0,
            total_visits INTEGER DEFAULT 0,
            redeemed_count INTEGER DEFAULT 0,
            push_token TEXT,
            push_token_auth TEXT,
            message TEXT,
            updated_at TEXT,
            last_push_status TEXT,
            last_sync_at TEXT
        )
    ''')
    # Migraciones seguras para columnas nuevas en bases de datos existentes
    cols = [r[1] for r in c.execute("PRAGMA table_info(clients)")]
    migrations = [
        ("message", "ALTER TABLE clients ADD COLUMN message TEXT"),
        ("phone", "ALTER TABLE clients ADD COLUMN phone TEXT"),
        ("birthdate", "ALTER TABLE clients ADD COLUMN birthdate TEXT"),
        ("last_push_status", "ALTER TABLE clients ADD COLUMN last_push_status TEXT"),
        ("last_sync_at", "ALTER TABLE clients ADD COLUMN last_sync_at TEXT"),
        ("total_visits", "ALTER TABLE clients ADD COLUMN total_visits INTEGER DEFAULT 0"),
        ("redeemed_count", "ALTER TABLE clients ADD COLUMN redeemed_count INTEGER DEFAULT 0"),
    ]
    for col_name, sql in migrations:
        if col_name not in cols:
            c.execute(sql)

    # Inicializar total_visits con el número actual de sellos para clientes existentes si está en 0
    c.execute("UPDATE clients SET total_visits = COALESCE(stamps, 0) WHERE total_visits IS NULL OR (total_visits = 0 AND stamps > 0)")
    c.execute("UPDATE clients SET redeemed_count = 0 WHERE redeemed_count IS NULL")
    c.execute("UPDATE clients SET updated_at = ? WHERE updated_at IS NULL OR updated_at = ''", (get_utc_timestamp(),))

    c.execute('''
        CREATE TABLE IF NOT EXISTS registrations (
            device_id TEXT,
            push_token TEXT,
            serial TEXT,
            PRIMARY KEY (device_id, serial)
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS wallet_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT,
            log_text TEXT
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    ''')
    # Valores iniciales de geolocalización de la cafetería
    default_settings = {
        "shop_lat": str(DEFAULT_LAT),
        "shop_lng": str(DEFAULT_LNG),
        "shop_relevant_text": DEFAULT_GEO_TEXT,
    }
    for k, v in default_settings.items():
        c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v))

    conn.commit()
    conn.close()


init_db()


def get_shop_location():
    """Obtiene la configuración de geolocalización GPS de la cafetería."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    rows = dict(c.execute("SELECT key, value FROM settings").fetchall())
    conn.close()
    try:
        lat = float(rows.get("shop_lat", DEFAULT_LAT))
    except (TypeError, ValueError):
        lat = DEFAULT_LAT
    try:
        lng = float(rows.get("shop_lng", DEFAULT_LNG))
    except (TypeError, ValueError):
        lng = DEFAULT_LNG
    relevant_text = rows.get("shop_relevant_text") or DEFAULT_GEO_TEXT
    return {"lat": lat, "lng": lng, "relevant_text": relevant_text}


REGISTRATION_HTML = """
<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Maishas Café - Tarjeta de Fidelización</title>
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background-color: #F3EDE2; color: #2C221C; display: flex; justify-content: center; align-items: center; min-height: 100vh; margin: 0; padding: 20px; box-sizing: border-box; }
        .card { background: white; padding: 30px; border-radius: 20px; box-shadow: 0 10px 30px rgba(0,0,0,0.1); width: 100%; max-width: 420px; text-align: center; }
        h1 { color: #2C221C; margin-bottom: 5px; font-size: 24px; }
        p.subtitle { color: #8A6646; font-size: 14px; margin-bottom: 22px; }
        .field-group { text-align: left; margin-bottom: 14px; }
        .field-label { display: block; font-size: 13px; font-weight: 600; color: #4A3B30; margin-bottom: 5px; }
        .optional-tag { font-weight: normal; color: #998A7E; font-size: 12px; }
        input { width: 100%; padding: 13px 14px; border: 1px solid #DDD; border-radius: 10px; font-size: 16px; box-sizing: border-box; font-family: inherit; color: #2C221C; background: #FAFAFA; }
        input:focus { outline: none; border-color: #8A6646; background: #FFF; }
        button { width: 100%; padding: 15px; background: #2C221C; color: #F3EDE2; border: none; border-radius: 10px; font-size: 16px; font-weight: bold; cursor: pointer; margin-top: 10px; }
        button:hover { background: #4A3B30; }
        .logo { font-size: 36px; margin-bottom: 8px; }
        .note { font-size: 12px; color: #8A6646; margin-top: 14px; }
        .perks-box { background: #F9F6F0; border-radius: 12px; padding: 10px 14px; margin-bottom: 18px; text-align: left; font-size: 12px; color: #554438; border: 1px solid #EAE0D5; }
    </style>
</head>
<body>
    <div class="card">
        <div class="logo">☕️🎃</div>
        <h1>Maishas Café</h1>
        <p class="subtitle">Regístrate para obtener tu Tarjeta de Fidelización en Apple Wallet</p>
        <div class="perks-box">
            ✨ <strong>Beneficios de tu tarjeta:</strong><br>
            • 1 Café Gratis al acumular 10 sellos<br>
            • Sube de nivel: <strong>Bronce 🌱 → Plata 🥈 → Oro VIP 👑</strong><br>
            • Sorpresas en tu cumpleaños y promociones exclusivas
        </div>
        <form action="/api/register" method="POST">
            <div class="field-group">
                <label class="field-label">Nombre y Apellido *</label>
                <input type="text" name="name" placeholder="Ej. Ana López" required>
            </div>
            <div class="field-group">
                <label class="field-label">Correo Electrónico *</label>
                <input type="email" name="email" placeholder="tu@correo.com" required>
            </div>
            <div class="field-group">
                <label class="field-label">Número de Celular <span class="optional-tag">(Opcional)</span></label>
                <input type="tel" name="phone" placeholder="Ej. 667 123 4567">
            </div>
            <div class="field-group">
                <label class="field-label">🎂 Fecha de Nacimiento <span class="optional-tag">(Opcional)</span></label>
                <input type="date" name="birthdate">
            </div>
            <button type="submit">Obtener Tarjeta en Apple Wallet 📲</button>
        </form>
        <p class="note">Ábrelo desde <strong>Safari en tu iPhone</strong> para agregarla directo a tu Wallet.</p>
    </div>
</body>
</html>
"""

BARISTA_HTML = """
<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Maishas Café - Panel Barista</title>
    <script src="https://cdn.jsdelivr.net/npm/jsqr@1.4.0/dist/jsQR.min.js"></script>
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background-color: #2C221C; color: #F3EDE2; padding: 20px; text-align: center; margin: 0; }
        .card { background: #3A2E26; padding: 20px; border-radius: 16px; max-width: 900px; margin: 0 auto 20px; box-shadow: 0 6px 20px rgba(0,0,0,0.25); text-align: left; }
        .card h3 { margin-top: 0; color: #F3EDE2; display: flex; align-items: center; justify-content: space-between; }
        button { padding: 8px 12px; background: #8A6646; color: white; border: none; border-radius: 8px; font-size: 13px; font-weight: 600; cursor: pointer; margin: 2px; }
        button:hover { opacity: 0.92; }
        .btn-primary { background: #e67e22; font-size: 15px; padding: 12px 18px; }
        .btn-sub { background: #554438; }
        .btn-redeem { background: #d35400; }
        .btn-del { background: #7f1d1d; padding: 8px 10px; }
        .btn-geo { background: #2980b9; }
        table { width: 100%; border-collapse: collapse; margin-top: 12px; font-size: 14px; }
        th, td { padding: 12px 10px; border-bottom: 1px solid #554438; text-align: left; vertical-align: middle; }
        th { color: #E8C39E; font-size: 12px; text-transform: uppercase; letter-spacing: 0.5px; }
        .stamps { font-weight: bold; color: #E8C39E; font-size: 16px; white-space: nowrap; }
        .visits-sub { font-size: 11px; color: #BBB; font-weight: normal; display: block; margin-top: 3px; }
        .badge { background: #27ae60; color: white; padding: 4px 8px; border-radius: 10px; font-size: 11px; display: inline-block; font-weight: 600; }
        .badge-off { background: #7f8c8d; color: white; padding: 4px 8px; border-radius: 10px; font-size: 11px; display: inline-block; }
        .tier-badge { padding: 3px 8px; border-radius: 8px; font-size: 11px; font-weight: 700; display: inline-block; margin-left: 6px; }
        textarea, select, input[type="text"], input[type="number"] { width: 100%; box-sizing: border-box; padding: 11px 12px; margin: 6px 0; border-radius: 10px; border: 1px solid #554438; background: #2C221C; color: #F3EDE2; font-size: 14px; font-family: inherit; }
        .hint { font-size: 12px; color: #BBB; margin: 6px 0 0; }
        .toast { background: #27ae60; color: white; padding: 14px 18px; font-weight: 600; border-radius: 12px; max-width: 900px; margin: 0 auto 20px; display: flex; justify-content: space-between; align-items: center; text-align: left; }
        .meta-info { color: #CCC; font-size: 12px; line-height: 1.4; margin-top: 3px; }
        .last-msg { color: #E8C39E; font-size: 11px; font-style: italic; display: inline-block; margin-top: 3px; }
        .sync-info { color: #A3E4D7; font-size: 11px; display: block; margin-top: 4px; }
        .push-info { color: #F5B041; font-size: 11px; display: block; margin-top: 2px; }
        .highlight-row { background-color: rgba(39, 174, 96, 0.22); transition: background-color 2s ease; }
        .actions-cell { white-space: nowrap; }
        .grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; max-width: 900px; margin: 0 auto 20px; }
        @media (max-width: 720px) { .grid-2 { grid-template-columns: 1fr; } }
        .grid-2 .card { margin: 0; width: 100%; box-sizing: border-box; }
        #scanner-box { display: none; margin-top: 12px; background: #1E1713; padding: 12px; border-radius: 12px; text-align: center; border: 1px solid #8A6646; }
        #qr-video { width: 100%; max-width: 360px; border-radius: 10px; background: #000; }
        .quick-scan-bar { display: flex; gap: 8px; align-items: center; margin-top: 8px; }
        .stats-bar { display: flex; gap: 12px; flex-wrap: wrap; margin-bottom: 12px; }
        .stat-pill { background: #2C221C; border: 1px solid #554438; border-radius: 10px; padding: 8px 14px; font-size: 13px; }
        .stat-pill strong { color: #E8C39E; font-size: 16px; }
        details summary { cursor: pointer; font-weight: 600; color: #E8C39E; outline: none; }
    </style>
</head>
<body>
    <h1>☕️ Panel Barista - Maishas Café</h1>
    {% if ok %}
    <div class="toast" id="status-toast">
        <span>{{ ok }}</span>
        <button onclick="document.getElementById('status-toast').style.display='none'" style="background:transparent; border:1px solid rgba(255,255,255,0.4); padding:4px 8px;">✕</button>
    </div>
    {% endif %}

    <div class="grid-2">
        <!-- 1. ESCÁNER QR EN VIVO Y LECTURA RÁPIDA -->
        <div class="card">
            <h3>
                <span>📷 Escáner QR de Clientes</span>
                <button type="button" id="btn-toggle-cam" class="btn-primary" onclick="toggleScanner()">📷 Abrir Cámara QR</button>
            </h3>
            <p class="hint" style="margin-bottom:10px;">Escanea el código QR del Apple Wallet del cliente con la cámara o con un lector físico para sumar su sello al instante.</p>

            <label style="font-size:13px; display:flex; align-items:center; gap:6px; cursor:pointer; margin-bottom:8px;">
                <input type="checkbox" id="auto-stamp-check" checked style="width:auto; margin:0;">
                ⚡ <strong>Modo Rápido:</strong> Sumar +1 sello automáticamente al detectar el QR
            </label>

            <div id="scanner-box">
                <video id="qr-video" playsinline muted></video>
                <canvas id="qr-canvas" style="display:none;"></canvas>
                <div id="qr-status" style="font-size:13px; color:#E8C39E; margin-top:8px;">Apunta al código QR de la tarjeta en el iPhone...</div>
            </div>

            <form id="scan-form" action="/api/scan-action" method="POST" class="quick-scan-bar">
                <input type="text" id="scan-serial-input" name="serial" placeholder="Código o Serial (ej. MAISHAS-AD58E5)" required style="margin:0;">
                <input type="hidden" name="action" id="scan-action-input" value="add">
                <button type="submit" style="white-space:nowrap; padding:11px 14px;">+1 Sello ☕️</button>
            </form>
        </div>

        <!-- 2. ENVIAR NOTIFICACIÓN PUSH -->
        <div class="card">
            <h3>📣 Enviar Notificación Push</h3>
            <form action="/api/send-message" method="POST">
                <textarea name="message" id="push-msg-box" maxlength="200" rows="2" placeholder="Ej: ¡Hoy 2x1 en lattes de 4 a 6 pm! ☕️🎃" required></textarea>
                <div style="margin: 4px 0 6px; display:flex; gap:4px; flex-wrap:wrap;">
                    <button type="button" class="btn-sub" style="font-size:11px; padding:4px 8px;" onclick="setTemplate('🥐 ¡Recién salidos del horno! Ven por tu pan calientito y suma tu sello de hoy ☕️')">🥐 Pan recién horneado</button>
                    <button type="button" class="btn-sub" style="font-size:11px; padding:4px 8px;" onclick="setTemplate('🎃 ¡Hora Feliz en Maishas Café! 2x1 en bebidas seleccionadas hoy de 4 a 6 pm 👻')">🎃 Hora Feliz 2x1</button>
                    <button type="button" class="btn-sub" style="font-size:11px; padding:4px 8px;" onclick="setTemplate('✨ ¡Hoy tus compras valen DOBLE sello en Maishas Café! Te esperamos ☕️')">✨ Doble Sello Hoy</button>
                </div>
                <select name="target">
                    <option value="all">📲 Todos los clientes ({{ installed_count }} iPhones activos)</option>
                    {% for client in clients %}
                    <option value="{{ client.serial }}">
                        {{ client.name }} ({{ client.serial }}) — {{ client.tier.badge }} — {{ client.stamps }}/10
                    </option>
                    {% endfor %}
                </select>
                <button type="submit" style="width:100%; padding:11px; font-size:14px; margin-top:4px;">Enviar Notificación Push 📲</button>
            </form>
        </div>
    </div>

    <!-- 3. GEOLOCALIZACIÓN GPS DE LA CAFETERÍA -->
    <div class="card">
        <details>
            <summary>📍 Geolocalización Inteligente en Apple Wallet (Alerta automática al pasar cerca de la cafetería) — Activa en ({{ "%.4f"|format(shop_loc.lat) }}, {{ "%.4f"|format(shop_loc.lng) }})</summary>
            <form action="/api/update-location" method="POST" style="margin-top:14px;">
                <p class="hint" style="margin-bottom:10px;">
                    Cuando tus clientes pasen a ~100 metros de estas coordenadas GPS, su iPhone encenderá la pantalla bloqueada automáticamente con este mensaje invitándolos a entrar.
                </p>
                <div style="display:grid; grid-template-columns: 1fr 1fr auto; gap:10px; align-items:end;">
                    <div>
                        <label style="font-size:12px; color:#E8C39E;">Latitud GPS</label>
                        <input type="text" id="lat-input" name="lat" value="{{ shop_loc.lat }}" required>
                    </div>
                    <div>
                        <label style="font-size:12px; color:#E8C39E;">Longitud GPS</label>
                        <input type="text" id="lng-input" name="lng" value="{{ shop_loc.lng }}" required>
                    </div>
                    <div style="padding-bottom:6px;">
                        <button type="button" class="btn-geo" onclick="detectMyGPS()" style="padding:11px 14px;">📍 Usar mi GPS actual</button>
                    </div>
                </div>
                <div style="margin-top:6px;">
                    <label style="font-size:12px; color:#E8C39E;">Mensaje en pantalla bloqueada al pasar cerca de la cafetería:</label>
                    <input type="text" name="relevant_text" value="{{ shop_loc.relevant_text }}" maxlength="160" required>
                </div>
                <button type="submit" style="margin-top:8px; padding:10px 18px;">💾 Guardar Coordenadas y Actualizar Tarjetas</button>
            </form>
        </details>
    </div>

    <!-- 4. TABLA DE CLIENTES CON NIVELES VIP Y BUSCADOR -->
    <div class="card">
        <h3>
            <span>👥 Clientes Registrados ({{ clients|length }})</span>
        </h3>
        <div class="stats-bar">
            <div class="stat-pill">📲 En iPhone: <strong>{{ installed_count }}</strong></div>
            <div class="stat-pill">👑 Socios Oro VIP: <strong>{{ gold_count }}</strong></div>
            <div class="stat-pill">🥈 Socios Plata: <strong>{{ silver_count }}</strong></div>
            <div class="stat-pill">🎁 Cafés Canjeados: <strong>{{ total_redeemed }}</strong></div>
        </div>

        <input type="text" id="client-search" placeholder="🔍 Buscar cliente por nombre, código MAISHAS, celular o correo..." oninput="filterClients()">

        <div style="overflow-x: auto;">
            <table id="clients-table">
                <thead>
                    <tr>
                        <th>Cliente / Nivel VIP</th>
                        <th>Estado en Apple Wallet</th>
                        <th>Sellos / Historial</th>
                        <th>Acciones por Usuario</th>
                    </tr>
                </thead>
                <tbody>
                    {% for client in clients %}
                    <tr id="row-{{ client.serial }}" class="client-row {{ 'highlight-row' if updated_serial == client.serial else '' }}" data-search="{{ (client.name ~ ' ' ~ client.serial ~ ' ' ~ client.email ~ ' ' ~ client.phone ~ ' ' ~ client.tier.badge)|lower }}">
                        <td>
                            <strong style="font-size:15px;">{{ client.name }}</strong>
                            <span class="tier-badge" style="background:{{ client.tier.bg }}; color:{{ client.tier.fg }};">{{ client.tier.badge }}</span>
                            <br>
                            <small style="color: #AAA; font-family: monospace;">{{ client.serial }}</small>
                            <div class="meta-info">
                                {% if client.email %}✉️ {{ client.email }}{% endif %}
                                {% if client.phone %}<br>📱 {{ client.phone }}{% endif %}
                                {% if client.birthdate %}<br>🎂 {{ client.birthdate }}{% endif %}
                            </div>
                            {% if client.message %}<span class="last-msg">💬 "{{ client.message }}"</span>{% endif %}
                        </td>
                        <td>
                            {% if client.is_installed %}
                            <span class="badge">📲 Instalado en iPhone ({{ client.device_count }})</span>
                            {% else %}
                            <span class="badge-off">⏳ Esperando instalación</span>
                            {% endif %}
                            {% if client.last_push_status %}
                            <span class="push-info">📡 Push: {{ client.last_push_status }}</span>
                            {% endif %}
                            {% if client.last_sync_at %}
                            <span class="sync-info">🔄 Sincronizado: {{ client.last_sync_at }}</span>
                            {% endif %}
                        </td>
                        <td class="stamps">
                            {{ client.stamps }} / 10
                            <span class="visits-sub">☕️ {{ client.total_visits }} visitas totales</span>
                            {% if client.redeemed_count > 0 %}
                            <span class="visits-sub" style="color:#F5B041;">🎁 {{ client.redeemed_count }} canjeado(s)</span>
                            {% endif %}
                        </td>
                        <td class="actions-cell">
                            <form action="/api/add-stamp" method="POST" style="display:inline;">
                                <input type="hidden" name="serial" value="{{ client.serial }}">
                                <input type="hidden" name="action" value="add">
                                <button type="submit" title="Sumar 1 sello a {{ client.name }}">+1 Sello ☕️</button>
                            </form>
                            {% if client.stamps > 0 %}
                            <form action="/api/add-stamp" method="POST" style="display:inline;">
                                <input type="hidden" name="serial" value="{{ client.serial }}">
                                <input type="hidden" name="action" value="sub">
                                <button type="submit" class="btn-sub" title="Restar 1 sello">-1</button>
                            </form>
                            <form action="/api/add-stamp" method="POST" style="display:inline;" onsubmit="return confirm('¿Canjear 1 Café Gratis y reiniciar a 0 sellos para {{ client.name }}?');">
                                <input type="hidden" name="serial" value="{{ client.serial }}">
                                <input type="hidden" name="action" value="reset">
                                <button type="submit" class="btn-redeem" title="Canjear café gratis y reiniciar a 0">🎁 Canjear</button>
                            </form>
                            {% endif %}
                            <form action="/api/delete-client" method="POST" style="display:inline;" onsubmit="return confirm('¿Eliminar el registro de {{ client.name }} ({{ client.serial }})?');">
                                <input type="hidden" name="serial" value="{{ client.serial }}">
                                <button type="submit" class="btn-del" title="Eliminar cliente">🗑️</button>
                            </form>
                        </td>
                    </tr>
                    {% else %}
                    <tr>
                        <td colspan="4" style="text-align:center; color:#AAA;">No hay clientes registrados aún.</td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
        </div>
    </div>

    <script>
        // Limpiar los parámetros ?ok=... de la URL para que nunca se queden pegados al recargar
        if (window.history.replaceState && window.location.search) {
            window.history.replaceState({}, document.title, window.location.pathname);
        }

        function setTemplate(text) {
            const box = document.getElementById('push-msg-box');
            if (box) {
                box.value = text;
                box.focus();
            }
        }

        function filterClients() {
            const q = (document.getElementById('client-search').value || '').toLowerCase().trim();
            const rows = document.querySelectorAll('.client-row');
            rows.forEach(row => {
                const text = row.getAttribute('data-search') || '';
                row.style.display = (!q || text.includes(q)) ? '' : 'none';
            });
        }

        function detectMyGPS() {
            if (!navigator.geolocation) {
                alert('Tu navegador no soporta geolocalización.');
                return;
            }
            navigator.geolocation.getCurrentPosition(
                pos => {
                    document.getElementById('lat-input').value = pos.coords.latitude.toFixed(6);
                    document.getElementById('lng-input').value = pos.coords.longitude.toFixed(6);
                },
                err => {
                    alert('No se pudo obtener la ubicación GPS: ' + err.message);
                },
                { enableHighAccuracy: true }
            );
        }

        // Lógica de Escáner QR en vivo con cámara
        let videoStream = null;
        let scanning = false;

        function playBeep() {
            try {
                const ctx = new (window.AudioContext || window.webkitAudioContext)();
                const osc = ctx.createOscillator();
                const gain = ctx.createGain();
                osc.type = 'sine';
                osc.frequency.setValueAtTime(880, ctx.currentTime);
                gain.gain.setValueAtTime(0.15, ctx.currentTime);
                osc.connect(gain);
                gain.connect(ctx.destination);
                osc.start();
                osc.stop(ctx.currentTime + 0.12);
            } catch (e) {}
        }

        async function toggleScanner() {
            const box = document.getElementById('scanner-box');
            const btn = document.getElementById('btn-toggle-cam');
            const video = document.getElementById('qr-video');

            if (scanning) {
                stopScanner();
                return;
            }

            try {
                videoStream = await navigator.mediaDevices.getUserMedia({
                    video: { facingMode: 'environment' }
                });
                video.srcObject = videoStream;
                await video.play();
                scanning = true;
                box.style.display = 'block';
                btn.textContent = '⏹️ Cerrar Cámara';
                requestAnimationFrame(scanTick);
            } catch (err) {
                alert('No se pudo abrir la cámara (asegúrate de dar permiso a la cámara en Safari/Chrome): ' + err.message);
            }
        }

        function stopScanner() {
            scanning = false;
            const box = document.getElementById('scanner-box');
            const btn = document.getElementById('btn-toggle-cam');
            if (videoStream) {
                videoStream.getTracks().forEach(t => t.stop());
                videoStream = null;
            }
            box.style.display = 'none';
            btn.textContent = '📷 Abrir Cámara QR';
        }

        function scanTick() {
            if (!scanning) return;
            const video = document.getElementById('qr-video');
            const canvas = document.getElementById('qr-canvas');
            const status = document.getElementById('qr-status');

            if (video.readyState === video.HAVE_ENOUGH_DATA) {
                canvas.width = video.videoWidth;
                canvas.height = video.videoHeight;
                const ctx = canvas.getContext('2d');
                ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
                const imageData = ctx.getImageData(0, 0, canvas.width, canvas.height);
                if (window.jsQR) {
                    const code = jsQR(imageData.data, imageData.width, imageData.height, {
                        inversionAttempts: 'dontInvert'
                    });
                    if (code && code.data) {
                        const detected = code.data.trim().toUpperCase();
                        if (detected.startsWith('MAISHAS-')) {
                            playBeep();
                            status.textContent = '✅ Código detectado: ' + detected;
                            document.getElementById('scan-serial-input').value = detected;
                            stopScanner();
                            if (document.getElementById('auto-stamp-check').checked) {
                                document.getElementById('scan-form').submit();
                            }
                            return;
                        }
                    }
                }
            }
            requestAnimationFrame(scanTick);
        }
    </script>
</body>
</html>
"""


def generate_signed_pkpass(
    serial_number,
    client_name,
    stamps_count,
    host_url,
    message=None,
    phone=None,
    birthdate=None,
    total_visits=None,
    redeemed_count=0
):
    temp_dir = tempfile.mkdtemp(prefix=f"pass_{serial_number}_")
    try:
        for item in os.listdir(PASS_TEMPLATE_DIR):
            if item in ["manifest.json", "signature", ".DS_Store"]:
                continue
            src = os.path.join(PASS_TEMPLATE_DIR, item)
            dst = os.path.join(temp_dir, item)
            if os.path.isfile(src):
                shutil.copy2(src, dst)

        pass_json_path = os.path.join(temp_dir, "pass.json")
        with open(pass_json_path, "r", encoding="utf-8") as f:
            pass_data = json.load(f)

        visits = total_visits if total_visits is not None else stamps_count
        tier = get_vip_tier(visits)

        pass_data["serialNumber"] = serial_number
        pass_data["barcodes"][0]["message"] = serial_number
        pass_data["barcodes"][0]["altText"] = serial_number
        pass_data["storeCard"]["headerFields"][0]["value"] = f"{stamps_count} / 10"
        pass_data["storeCard"]["secondaryFields"][0]["value"] = client_name

        # 🏆 Nivel de Socio VIP dinámico en primaryFields (con alerta cuando sube de nivel)
        pass_data["storeCard"]["primaryFields"][0]["value"] = tier["name"]
        pass_data["storeCard"]["primaryFields"][0]["changeMessage"] = "🏆 ¡Subiste de nivel en Maishas Café! Ahora eres: %@"

        # 📍 Geolocalización inteligente: muestra la tarjeta en la pantalla bloqueada al pasar cerca
        shop_loc = get_shop_location()
        pass_data["locations"] = [
            {
                "latitude": shop_loc["lat"],
                "longitude": shop_loc["lng"],
                "relevantText": shop_loc["relevant_text"]
            }
        ]
        pass_data["maxDistance"] = 150

        # 🔔 Configuración del Web Service para Notificaciones Push (APNs)
        clean_host = host_url.rstrip("/")
        if not ("localhost" in clean_host or "127.0.0.1" in clean_host):
            clean_host = clean_host.replace("http://", "https://", 1)
        pass_data["webServiceURL"] = f"{clean_host}/"
        pass_data["authenticationToken"] = AUTH_TOKEN

        # Mensaje dinámico en pantalla bloqueada según los sellos
        if stamps_count == 0:
            stamp_change_msg = "🎁 ¡Sellos actualizados en Maishas Café! Total: %@"
        elif stamps_count >= 10:
            stamp_change_msg = "🎉 ¡Felicidades! Completaste %@ sellos. ¡Tienes 1 Café Gratis en Maishas Café!"
        else:
            stamp_change_msg = "¡Has ganado 1 nuevo sello en Maishas Café! ☕️ Total: %@"
        pass_data["storeCard"]["headerFields"][0]["changeMessage"] = stamp_change_msg

        # Actualizar recompensa y mostrar aviso también al frente de la tarjeta
        reward_text = "¡1 Café Gratis Disponible! 🎉" if stamps_count >= 10 else "1 Café Gratis 👻"
        clean_visible_msg = (message or DEFAULT_MESSAGE).replace("\u200b", "")
        pass_data["storeCard"]["auxiliaryFields"] = [
            {
                "key": "reward",
                "label": "PRÓXIMA RECOMPENSA",
                "value": reward_text
            },
            {
                "key": "promo",
                "label": "AVISO MAISHAS",
                "value": clean_visible_msg
            }
        ]

        # 📣 Mensaje del barista en el reverso con changeMessage para disparar alerta Push en iOS
        pass_data["storeCard"]["backFields"].insert(0, {
            "key": "news",
            "label": "Mensaje de Maishas Café",
            "value": message or DEFAULT_MESSAGE,
            "changeMessage": "📣 %@"
        })

        # 🏆 Historial de visitas y nivel VIP en el reverso
        pass_data["storeCard"]["backFields"].insert(1, {
            "key": "vipHistory",
            "label": "Historial y Nivel de Socio",
            "value": f"Nivel: {tier['name']} | Visitas históricas: {visits} | Cafés gratis canjeados: {redeemed_count} ({tier['next_goal']})"
        })

        # Datos opcionales del socio en el reverso si fueron proporcionados
        extra_info = []
        if phone:
            extra_info.append(f"Celular: {phone}")
        if birthdate:
            extra_info.append(f"Cumpleaños: {birthdate}")
        if extra_info:
            pass_data["storeCard"]["backFields"].insert(2, {
                "key": "memberDetails",
                "label": "Datos del Cliente",
                "value": " | ".join(extra_info)
            })

        with open(pass_json_path, "w", encoding="utf-8") as f:
            json.dump(pass_data, f, indent=2)

        # Manifest SHA1
        manifest = {}
        for root, dirs, files in os.walk(temp_dir):
            for fname in files:
                if fname in ['manifest.json', 'signature', '.DS_Store']:
                    continue
                full_path = os.path.join(root, fname)
                rel_path = os.path.relpath(full_path, temp_dir).replace('\\', '/')
                with open(full_path, 'rb') as fp:
                    manifest[rel_path] = hashlib.sha1(fp.read()).hexdigest()

        manifest_path = os.path.join(temp_dir, "manifest.json")
        with open(manifest_path, "w", encoding="utf-8") as fp:
            json.dump(manifest, fp, indent=2)

        # Firmar con OpenSSL
        sig_path = os.path.join(temp_dir, "signature")
        cmd_sign = (
            f'openssl smime -sign -signer "{CERT_PEM}" -inkey "{KEY_PEM}" '
            f'-certfile "{WWDR_PEM}" -in "{manifest_path}" -out "{sig_path}" '
            f'-outform DER -binary'
        )
        subprocess.run(cmd_sign, shell=True, check=True)

        output_pkpass = f"/tmp/{serial_number}.pkpass"
        with zipfile.ZipFile(output_pkpass, 'w', zipfile.ZIP_DEFLATED) as z:
            for root, dirs, files in os.walk(temp_dir):
                for fname in files:
                    if fname == '.DS_Store':
                        continue
                    full_path = os.path.join(root, fname)
                    rel_path = os.path.relpath(full_path, temp_dir)
                    z.write(full_path, rel_path)

        return output_pkpass
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


# ---------------------------------------------------------
# RUTA PRINCIPAL Y BARISTA
# ---------------------------------------------------------

@app.route("/")
def index():
    return render_template_string(REGISTRATION_HTML)


@app.route("/barista")
@requires_auth
def barista():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    # Ordenar por rowid DESC (orden fijo de creación) para que las filas NUNCA salten al actualizar sellos
    c.execute("""
        SELECT c.serial, c.name, c.email, c.stamps, c.push_token, c.message,
               c.phone, c.birthdate, c.last_push_status, c.last_sync_at,
               (SELECT COUNT(*) FROM registrations r WHERE r.serial = c.serial) as reg_count,
               c.total_visits, c.redeemed_count
        FROM clients c
        ORDER BY c.rowid DESC
    """)
    rows = c.fetchall()
    conn.close()

    clients = []
    installed_count = 0
    gold_count = 0
    silver_count = 0
    total_redeemed = 0

    for r in rows:
        reg_count = r[10] or 0
        is_installed = bool(r[4]) or reg_count > 0
        if is_installed:
            installed_count += 1
        stamps_val = r[3] or 0
        visits_val = r[11] if r[11] is not None else stamps_val
        redeemed_val = r[12] or 0
        total_redeemed += redeemed_val
        tier = get_vip_tier(visits_val)
        if visits_val >= 25:
            gold_count += 1
        elif visits_val >= 10:
            silver_count += 1

        clients.append({
            "serial": r[0],
            "name": r[1],
            "email": r[2],
            "stamps": stamps_val,
            "push_token": r[4],
            "message": (r[5] or "").replace("\u200b", ""),
            "phone": r[6] or "",
            "birthdate": r[7] or "",
            "last_push_status": r[8] or "",
            "last_sync_at": r[9] or "",
            "device_count": max(reg_count, 1 if r[4] else 0),
            "is_installed": is_installed,
            "total_visits": visits_val,
            "redeemed_count": redeemed_val,
            "tier": tier,
        })

    return render_template_string(
        BARISTA_HTML,
        clients=clients,
        installed_count=installed_count,
        gold_count=gold_count,
        silver_count=silver_count,
        total_redeemed=total_redeemed,
        shop_loc=get_shop_location(),
        ok=request.args.get("ok"),
        updated_serial=request.args.get("updated")
    )


@app.route("/api/register", methods=["POST"])
def register():
    name = (request.form.get("name") or "").strip()
    email = (request.form.get("email") or "").strip()
    phone = (request.form.get("phone") or "").strip()
    birthdate = (request.form.get("birthdate") or "").strip()
    serial_number = f"MAISHAS-{uuid.uuid4().hex[:6].upper()}"
    now_ts = get_utc_timestamp()

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        """INSERT INTO clients (serial, name, email, phone, birthdate, stamps, total_visits, redeemed_count, message, updated_at)
           VALUES (?, ?, ?, ?, ?, 0, 0, 0, ?, ?)""",
        (serial_number, name, email, phone, birthdate, DEFAULT_MESSAGE, now_ts)
    )
    conn.commit()
    conn.close()

    host_url = request.host_url
    pkpass_path = generate_signed_pkpass(
        serial_number, name, 0, host_url, DEFAULT_MESSAGE,
        phone=phone, birthdate=birthdate, total_visits=0, redeemed_count=0
    )
    safe_name = "".join(ch if ch.isalnum() else "_" for ch in name) or "Cliente"
    return send_file(
        pkpass_path,
        mimetype="application/vnd.apple.pkpass",
        as_attachment=True,
        download_name=f"MaishasCafe_{safe_name}.pkpass"
    )


def _perform_stamp_update(serial, action="add"):
    """Función auxiliar compartida entre el botón de la tabla y el escáner QR."""
    serial_clean = (serial or "").strip().upper()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT serial, name, stamps, total_visits, redeemed_count FROM clients WHERE UPPER(serial) = ?",
        (serial_clean,)
    )
    client = c.fetchone()

    if not client:
        conn.close()
        return None, f"⚠️ Código QR / Cliente '{serial_clean}' no encontrado"

    serial_id, client_name = client[0], client[1]
    current_stamps = client[2] or 0
    current_visits = client[3] if client[3] is not None else current_stamps
    current_redeemed = client[4] or 0

    if action == "sub":
        new_stamps = max(0, current_stamps - 1)
        new_visits = max(0, current_visits - 1)
        new_redeemed = current_redeemed
        action_label = f"Sello restado a {client_name} ({serial_id}): {new_stamps}/10"
    elif action == "reset":
        new_stamps = 0
        new_visits = current_visits
        new_redeemed = current_redeemed + 1
        action_label = f"🎁 Café Gratis canjeado para {client_name} ({serial_id}): reiniciado a 0/10 (Total canjes: {new_redeemed})"
    else:
        if current_stamps >= 10:
            conn.close()
            return serial_id, f"ℹ️ {client_name} ({serial_id}) ya tiene 10/10 sellos. Usa '🎁 Canjear' para entregar su café gratis."
        new_stamps = current_stamps + 1
        new_visits = current_visits + 1
        new_redeemed = current_redeemed
        old_tier = get_vip_tier(current_visits)["name"]
        new_tier = get_vip_tier(new_visits)["name"]
        tier_note = f" — 🏆 ¡Subió a {new_tier}!" if new_tier != old_tier else ""
        action_label = f"✅ Sello agregado a {client_name} ({serial_id}): ahora tiene {new_stamps}/10 ({new_visits} visitas){tier_note}"

    now_ts = get_utc_timestamp()
    c.execute(
        "UPDATE clients SET stamps = ?, total_visits = ?, redeemed_count = ?, updated_at = ? WHERE serial = ?",
        (new_stamps, new_visits, new_redeemed, now_ts, serial_id)
    )
    conn.commit()
    conn.close()

    push_summary = send_apns_push_for_serial(serial_id)
    return serial_id, f"{action_label} — {push_summary}"


@app.route("/api/add-stamp", methods=["POST"])
@requires_auth
def add_stamp():
    serial = request.form.get("serial")
    action = request.form.get("action", "add")
    serial_id, msg = _perform_stamp_update(serial, action)
    return redirect(url_for("barista", ok=msg, updated=serial_id))


@app.route("/api/scan-action", methods=["POST"])
@requires_auth
def scan_action():
    serial = request.form.get("serial")
    action = request.form.get("action", "add")
    serial_id, msg = _perform_stamp_update(serial, action)
    return redirect(url_for("barista", ok=f"📷 QR Escaneado: {msg}", updated=serial_id))


@app.route("/api/update-location", methods=["POST"])
@requires_auth
def update_location():
    lat_raw = (request.form.get("lat") or "").strip()
    lng_raw = (request.form.get("lng") or "").strip()
    relevant_text = (request.form.get("relevant_text") or "").strip()[:160] or DEFAULT_GEO_TEXT
    try:
        lat = float(lat_raw)
        lng = float(lng_raw)
    except ValueError:
        return redirect(url_for("barista", ok="⚠️ Latitud y Longitud deben ser números válidos (ej. 24.8049, -107.3940)"))

    now_ts = get_utc_timestamp()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('shop_lat', ?)", (str(lat),))
    c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('shop_lng', ?)", (str(lng),))
    c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('shop_relevant_text', ?)", (relevant_text,))
    # Actualizar timestamp de todos los clientes para que sus iPhones descarguen las nuevas coordenadas GPS
    c.execute("UPDATE clients SET updated_at = ?", (now_ts,))
    serials = [r[0] for r in c.execute("SELECT serial FROM clients").fetchall()]
    conn.commit()
    conn.close()

    pushed = 0
    for s in serials:
        res = send_apns_push_for_serial(s)
        if "200 OK" in res:
            pushed += 1

    return redirect(url_for(
        "barista",
        ok=f"📍 Ubicación GPS guardada ({lat:.5f}, {lng:.5f}) y enviada a {pushed} iPhone(s) activos"
    ))


@app.route("/api/delete-client", methods=["POST"])
@requires_auth
def delete_client():
    serial = request.form.get("serial")
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT name FROM clients WHERE serial = ?", (serial,))
    row = c.fetchone()
    c.execute("DELETE FROM registrations WHERE serial = ?", (serial,))
    c.execute("DELETE FROM clients WHERE serial = ?", (serial,))
    conn.commit()
    conn.close()
    name = row[0] if row else serial
    return redirect(url_for("barista", ok=f"🗑️ Cliente {name} ({serial}) eliminado"))


@app.route("/api/send-message", methods=["POST"])
@requires_auth
def send_message():
    raw_message = (request.form.get("message") or "").strip()[:200]
    target = request.form.get("target", "all")
    if not raw_message:
        return redirect(url_for("barista", ok="⚠️ Escribe un mensaje antes de enviar"))

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    if target == "all":
        c.execute("SELECT serial, name, message FROM clients")
    else:
        c.execute("SELECT serial, name, message FROM clients WHERE serial = ?", (target,))
    rows = c.fetchall()

    now_ts = get_utc_timestamp()
    for serial_id, _, prev_msg in rows:
        # Si el barista envía el mismo texto dos veces seguidas, alternamos un espacio invisible (\u200b)
        # para que iOS detecte un cambio en pass.json y SIEMPRE muestre la notificación en pantalla bloqueada
        msg_to_save = raw_message
        if prev_msg == raw_message:
            msg_to_save = raw_message + "\u200b"
        c.execute(
            "UPDATE clients SET message = ?, updated_at = ? WHERE serial = ?",
            (msg_to_save, now_ts, serial_id)
        )
    conn.commit()
    conn.close()

    pushed_ok = 0
    last_res = ""
    for serial_id, _, _ in rows:
        last_res = send_apns_push_for_serial(serial_id)
        if "200 OK" in last_res:
            pushed_ok += 1

    if target == "all":
        msg_status = f"📣 Mensaje enviado: {pushed_ok} cliente(s) con iPhone notificados por Push APNs"
        return redirect(url_for("barista", ok=msg_status))
    else:
        target_name = rows[0][1] if rows else target
        return redirect(url_for("barista", ok=f"📣 Mensaje enviado a {target_name} ({target}) — {last_res}", updated=target))


# ---------------------------------------------------------
# APPLE WALLET WEB SERVICE PROTOCOL (APNs & REST API)
# Especificación: developer.apple.com/documentation/walletpasses
# ---------------------------------------------------------

def is_authorized():
    return request.headers.get("Authorization", "") == f"ApplePass {AUTH_TOKEN}"


# 1. Registro del dispositivo (cuando el usuario agrega la tarjeta a su iPhone)
@app.route("/v1/devices/<device_id>/registrations/<pass_type_id>/<serial_number>", methods=["POST"])
def register_device(device_id, pass_type_id, serial_number):
    if not is_authorized():
        return "", 401
    push_token = (request.get_json(silent=True) or {}).get("pushToken")
    if not push_token:
        return "", 400

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT 1 FROM registrations WHERE device_id = ? AND serial = ?", (device_id, serial_number))
    exists = c.fetchone()
    c.execute(
        "INSERT OR REPLACE INTO registrations (device_id, push_token, serial) VALUES (?, ?, ?)",
        (device_id, push_token, serial_number)
    )
    c.execute(
        "UPDATE clients SET push_token = ?, last_sync_at = ? WHERE serial = ?",
        (push_token, get_short_time(), serial_number)
    )
    conn.commit()
    conn.close()

    print(f"📲 iPhone registrado: device={device_id} serial={serial_number}")
    return "", (200 if exists else 201)


# 2. Baja del dispositivo (cuando el usuario elimina la tarjeta)
@app.route("/v1/devices/<device_id>/registrations/<pass_type_id>/<serial_number>", methods=["DELETE"])
def unregister_device(device_id, pass_type_id, serial_number):
    if not is_authorized():
        return "", 401
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM registrations WHERE device_id = ? AND serial = ?", (device_id, serial_number))
    conn.execute(
        "UPDATE clients SET push_token = NULL WHERE serial = ? AND serial NOT IN (SELECT serial FROM registrations)",
        (serial_number,)
    )
    conn.commit()
    conn.close()
    return "", 200


# 3. Lista de pases actualizados para un dispositivo
@app.route("/v1/devices/<device_id>/registrations/<pass_type_id>", methods=["GET"])
def get_updated_passes(device_id, pass_type_id):
    since = request.args.get("passesUpdatedSince")
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    query = """SELECT c.serial, c.updated_at FROM clients c
               JOIN registrations r ON r.serial = c.serial
               WHERE r.device_id = ?"""
    params = [device_id]
    if since:
        query += " AND c.updated_at > ?"
        params.append(since)
    c.execute(query, params)
    rows = c.fetchall()
    conn.close()

    if not rows:
        return "", 204
    return jsonify({
        "serialNumbers": [r[0] for r in rows],
        "lastUpdated": max((r[1] for r in rows if r[1]), default=get_utc_timestamp())
    })


# 4. Descarga del pase actualizado (el iPhone baja la versión nueva con los sellos/mensajes/nivel/GPS)
@app.route("/v1/passes/<pass_type_id>/<serial_number>", methods=["GET"])
def get_latest_pass(pass_type_id, serial_number):
    if not is_authorized():
        return "", 401
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT name, stamps, message, phone, birthdate, total_visits, redeemed_count, updated_at FROM clients WHERE serial = ?",
        (serial_number,)
    )
    client = c.fetchone()
    if not client:
        conn.close()
        return "", 404

    name, stamps, message, phone, birthdate, total_visits, redeemed_count, updated_at_str = client

    # Convertir updated_at a epoch UTC para manejo exacto de Last-Modified e If-Modified-Since
    try:
        dt_upd = datetime.strptime(updated_at_str, "%Y-%m-%d %H:%M:%S.%f").replace(tzinfo=timezone.utc)
    except Exception:
        try:
            dt_upd = datetime.strptime(updated_at_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except Exception:
            dt_upd = datetime.now(timezone.utc)

    upd_epoch_sec = int(dt_upd.timestamp())
    ims_header = request.headers.get("If-Modified-Since")
    if ims_header:
        from email.utils import parsedate_to_datetime
        try:
            ims_dt = parsedate_to_datetime(ims_header)
            if int(ims_dt.timestamp()) > upd_epoch_sec:
                conn.close()
                return "", 304
        except Exception:
            pass

    # Registrar el momento exacto en que el iPhone descargó la actualización
    c.execute("UPDATE clients SET last_sync_at = ? WHERE serial = ?", (get_short_time(), serial_number))
    conn.commit()
    conn.close()

    pkpass_path = generate_signed_pkpass(
        serial_number, name, stamps or 0, request.host_url, message,
        phone=phone, birthdate=birthdate, total_visits=total_visits, redeemed_count=redeemed_count or 0
    )
    response = send_file(pkpass_path, mimetype="application/vnd.apple.pkpass", conditional=False, max_age=0)
    response.headers["Last-Modified"] = formatdate(timeval=float(upd_epoch_sec), localtime=False, usegmt=True)
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response


# 5. Log de errores que reporta el iPhone (útil para depurar)
@app.route("/v1/log", methods=["POST"])
def wallet_log():
    logs = (request.get_json(silent=True) or {}).get("logs", [])
    if logs:
        conn = sqlite3.connect(DB_PATH)
        for line in logs:
            print(f"📝 Wallet log: {line}")
            conn.execute("INSERT INTO wallet_logs (created_at, log_text) VALUES (?, ?)", (get_utc_timestamp(), str(line)))
        conn.commit()
        conn.close()
    return "", 200


# 6. Envío de Push a Apple APNs (a todos los iPhones registrados para ese pase, sin duplicados)
def send_apns_push_for_serial(serial):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    raw_tokens = [r[0] for r in c.execute("SELECT push_token FROM registrations WHERE serial = ? AND push_token IS NOT NULL", (serial,)).fetchall()]
    client_row = c.execute("SELECT push_token FROM clients WHERE serial = ?", (serial,)).fetchone()
    if client_row and client_row[0]:
        raw_tokens.append(client_row[0])
    # Deduplicar tokens manteniendo el orden para nunca enviar doble push al mismo iPhone
    reg_tokens = list(dict.fromkeys(t for t in raw_tokens if t))

    if not reg_tokens:
        status_msg = "⏳ Sin iPhone registrado aún"
        c.execute("UPDATE clients SET last_push_status = ? WHERE serial = ?", (status_msg, serial))
        conn.commit()
        conn.close()
        return status_msg

    results = []
    for token in reg_tokens:
        ok, status_code, detail = send_apns_push(token)
        if ok:
            results.append(f"✅ 200 OK ({get_short_time()})")
        else:
            results.append(f"⚠️ Error {status_code}: {detail}")
            if status_code == 410 or "Unregistered" in detail or "BadDeviceToken" in detail:
                c.execute("DELETE FROM registrations WHERE push_token = ? AND serial = ?", (token, serial))

    summary = " | ".join(results)
    c.execute("UPDATE clients SET last_push_status = ? WHERE serial = ?", (summary, serial))
    conn.commit()
    conn.close()
    return summary


def send_apns_push(push_token):
    try:
        url = f"https://api.push.apple.com/3/device/{push_token}"
        context = ssl.create_default_context()
        context.load_cert_chain(certfile=CERT_PEM, keyfile=KEY_PEM)
        with httpx.Client(http2=True, verify=context, timeout=10.0) as client:
            headers = {
                "apns-topic": PASS_TYPE_ID,
                "apns-priority": "10"
            }
            res = client.post(url, json={}, headers=headers)
            print(f"📡 Push APNs enviado a {push_token[:12]}... Status: {res.status_code} {res.text}")
            if res.status_code == 200:
                return True, 200, "OK"
            return False, res.status_code, res.text
    except Exception as e:
        print(f"⚠️ Error enviando Push APNs: {e}")
        return False, 500, str(e)


# 7. Diagnóstico interno completo para verificar salud del sistema
@app.route("/api/diagnostics")
@requires_auth
def diagnostics():
    report = {
        "certificates": {
            "cert_pem_exists": os.path.isfile(CERT_PEM),
            "key_pem_exists": os.path.isfile(KEY_PEM),
            "wwdr_pem_exists": os.path.isfile(WWDR_PEM),
        },
        "http2_support": False,
        "apns_connection": None,
        "pass_signing": False,
        "geolocation": get_shop_location(),
        "database": {},
        "recent_wallet_logs": []
    }
    try:
        import h2  # noqa: F401
        report["http2_support"] = True
    except ImportError as e:
        report["http2_error"] = str(e)

    try:
        test_path = generate_signed_pkpass("MAISHAS-DIAG01", "Prueba Diag", 1, request.host_url, "Diagnóstico OK", total_visits=12)
        report["pass_signing"] = os.path.isfile(test_path) and os.path.getsize(test_path) > 1000
    except Exception as e:
        report["pass_signing_error"] = str(e)

    try:
        ok, code, detail = send_apns_push("0000000000000000000000000000000000000000000000000000000000000000")
        report["apns_connection"] = {
            "reachable_and_authenticated": code in (200, 400),
            "status_code": code,
            "response": detail
        }
    except Exception as e:
        report["apns_connection"] = {"reachable_and_authenticated": False, "error": str(e)}

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    report["database"]["total_clients"] = c.execute("SELECT COUNT(*) FROM clients").fetchone()[0]
    report["database"]["total_registrations"] = c.execute("SELECT COUNT(*) FROM registrations").fetchone()[0]
    report["recent_wallet_logs"] = [
        {"time": r[0], "log": r[1]}
        for r in c.execute("SELECT created_at, log_text FROM wallet_logs ORDER BY id DESC LIMIT 20").fetchall()
    ]
    conn.close()
    return jsonify(report)


if __name__ == "__main__":
    print("🚀 Servidor con soporte APNs Push iniciado en http://localhost:5001")
    port = int(os.environ.get('PORT', 5001))
    app.run(host="0.0.0.0", port=port, debug=False)
