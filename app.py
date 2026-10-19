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


def get_utc_timestamp():
    """Retorna timestamp UTC con precisión de microsegundos compatible con SQLite."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")


def get_short_time():
    """Retorna hora legible UTC para mostrar en el panel."""
    return datetime.now(timezone.utc).strftime("%H:%M:%S UTC")


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
    ]
    for col_name, sql in migrations:
        if col_name not in cols:
            c.execute(sql)

    # Asegurar que ningún registro antiguo tenga updated_at nulo
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
    conn.commit()
    conn.close()


init_db()

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
    </style>
</head>
<body>
    <div class="card">
        <div class="logo">☕️🎃</div>
        <h1>Maishas Café</h1>
        <p class="subtitle">Regístrate para obtener tu Tarjeta de Fidelización en Apple Wallet</p>
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
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background-color: #2C221C; color: #F3EDE2; padding: 20px; text-align: center; margin: 0; }
        .card { background: #3A2E26; padding: 20px; border-radius: 16px; max-width: 820px; margin: 0 auto 20px; box-shadow: 0 6px 20px rgba(0,0,0,0.25); }
        button { padding: 8px 12px; background: #8A6646; color: white; border: none; border-radius: 8px; font-size: 13px; font-weight: 600; cursor: pointer; margin: 2px; }
        button:hover { opacity: 0.9; }
        .btn-sub { background: #554438; }
        .btn-redeem { background: #d35400; }
        .btn-del { background: #7f1d1d; padding: 8px 10px; }
        table { width: 100%; border-collapse: collapse; margin-top: 15px; font-size: 14px; }
        th, td { padding: 12px 10px; border-bottom: 1px solid #554438; text-align: left; vertical-align: middle; }
        th { color: #E8C39E; font-size: 12px; text-transform: uppercase; letter-spacing: 0.5px; }
        .stamps { font-weight: bold; color: #E8C39E; font-size: 16px; white-space: nowrap; }
        .badge { background: #27ae60; color: white; padding: 4px 8px; border-radius: 10px; font-size: 11px; display: inline-block; font-weight: 600; }
        .badge-off { background: #7f8c8d; color: white; padding: 4px 8px; border-radius: 10px; font-size: 11px; display: inline-block; }
        textarea, select { width: 100%; box-sizing: border-box; padding: 12px; margin: 6px 0; border-radius: 10px; border: 1px solid #554438; background: #2C221C; color: #F3EDE2; font-size: 15px; font-family: inherit; }
        .hint { font-size: 12px; color: #BBB; margin: 8px 0 0; }
        .toast { background: #27ae60; color: white; padding: 14px 18px; font-weight: 600; border-radius: 12px; max-width: 820px; margin: 0 auto 20px; display: flex; justify-content: space-between; align-items: center; }
        .meta-info { color: #CCC; font-size: 12px; line-height: 1.4; margin-top: 3px; }
        .last-msg { color: #E8C39E; font-size: 11px; font-style: italic; display: inline-block; margin-top: 3px; }
        .sync-info { color: #A3E4D7; font-size: 11px; display: block; margin-top: 4px; }
        .push-info { color: #F5B041; font-size: 11px; display: block; margin-top: 2px; }
        .highlight-row { background-color: rgba(39, 174, 96, 0.18); transition: background-color 2s ease; }
        .actions-cell { white-space: nowrap; }
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

    <div class="card">
        <h3>📣 Enviar notificación Push a clientes</h3>
        <form action="/api/send-message" method="POST">
            <textarea name="message" maxlength="200" rows="2" placeholder="Ej: ¡Hoy 2x1 en lattes de 4 a 6 pm! ☕️🎃" required></textarea>
            <select name="target">
                <option value="all">📲 Todos los clientes con tarjeta instalada ({{ installed_count }} iPhones activos)</option>
                {% for client in clients %}
                <option value="{{ client.serial }}">
                    {{ client.name }} ({{ client.serial }}) — {{ '📲 En iPhone' if client.is_installed else '⏳ Sin instalar' }} — {{ client.stamps }}/10 sellos
                </option>
                {% endfor %}
            </select>
            <button type="submit" style="width:100%; padding:12px; font-size:15px; margin-top:6px;">Enviar notificación Push 📲</button>
        </form>
        <p class="hint">Envía una alerta inmediata a la pantalla bloqueada del iPhone y actualiza el aviso en su tarjeta.</p>
    </div>

    <div class="card">
        <h3>👥 Clientes Registrados ({{ clients|length }})</h3>
        <div style="overflow-x: auto;">
            <table>
                <thead>
                    <tr>
                        <th>Cliente / Datos</th>
                        <th>Estado en Apple Wallet</th>
                        <th>Sellos</th>
                        <th>Acciones por Usuario</th>
                    </tr>
                </thead>
                <tbody>
                    {% for client in clients %}
                    <tr id="row-{{ client.serial }}" class="{{ 'highlight-row' if updated_serial == client.serial else '' }}">
                        <td>
                            <strong style="font-size:15px;">{{ client.name }}</strong>
                            <small style="color: #AAA; margin-left:4px;">({{ client.serial }})</small>
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
                        <td class="stamps">{{ client.stamps }} / 10</td>
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
                            <form action="/api/add-stamp" method="POST" style="display:inline;" onsubmit="return confirm('¿Canjear recompensa y reiniciar a 0 sellos para {{ client.name }}?');">
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
        // Limpiar los parámetros ?ok=... de la URL para que no se queden pegados al recargar
        if (window.history.replaceState && window.location.search) {
            window.history.replaceState({}, document.title, window.location.pathname);
        }
    </script>
</body>
</html>
"""


def generate_signed_pkpass(serial_number, client_name, stamps_count, host_url, message=None, phone=None, birthdate=None):
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

        pass_data["serialNumber"] = serial_number
        pass_data["barcodes"][0]["message"] = serial_number
        pass_data["barcodes"][0]["altText"] = serial_number
        pass_data["storeCard"]["headerFields"][0]["value"] = f"{stamps_count} / 10"
        pass_data["storeCard"]["secondaryFields"][0]["value"] = client_name

        # 🔔 Configuración del Web Service para Notificaciones Push (APNs)
        # Apple Wallet exige HTTPS para registrar el dispositivo y recibir actualizaciones
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

        # Datos opcionales del socio en el reverso si fueron proporcionados
        extra_info = []
        if phone:
            extra_info.append(f"Celular: {phone}")
        if birthdate:
            extra_info.append(f"Cumpleaños: {birthdate}")
        if extra_info:
            pass_data["storeCard"]["backFields"].insert(1, {
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
               (SELECT COUNT(*) FROM registrations r WHERE r.serial = c.serial) as reg_count
        FROM clients c
        ORDER BY c.rowid DESC
    """)
    rows = c.fetchall()
    conn.close()

    clients = []
    installed_count = 0
    for r in rows:
        reg_count = r[10] or 0
        is_installed = bool(r[4]) or reg_count > 0
        if is_installed:
            installed_count += 1
        clients.append({
            "serial": r[0],
            "name": r[1],
            "email": r[2],
            "stamps": r[3] or 0,
            "push_token": r[4],
            "message": (r[5] or "").replace("\u200b", ""),
            "phone": r[6] or "",
            "birthdate": r[7] or "",
            "last_push_status": r[8] or "",
            "last_sync_at": r[9] or "",
            "device_count": max(reg_count, 1 if r[4] else 0),
            "is_installed": is_installed,
        })

    return render_template_string(
        BARISTA_HTML,
        clients=clients,
        installed_count=installed_count,
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
        """INSERT INTO clients (serial, name, email, phone, birthdate, stamps, message, updated_at)
           VALUES (?, ?, ?, ?, ?, 0, ?, ?)""",
        (serial_number, name, email, phone, birthdate, DEFAULT_MESSAGE, now_ts)
    )
    conn.commit()
    conn.close()

    host_url = request.host_url
    pkpass_path = generate_signed_pkpass(
        serial_number, name, 0, host_url, DEFAULT_MESSAGE, phone=phone, birthdate=birthdate
    )
    safe_name = "".join(ch if ch.isalnum() else "_" for ch in name) or "Cliente"
    return send_file(
        pkpass_path,
        mimetype="application/vnd.apple.pkpass",
        as_attachment=True,
        download_name=f"MaishasCafe_{safe_name}.pkpass"
    )


@app.route("/api/add-stamp", methods=["POST"])
@requires_auth
def add_stamp():
    serial = request.form.get("serial")
    action = request.form.get("action", "add")

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT serial, name, stamps FROM clients WHERE serial = ?", (serial,))
    client = c.fetchone()

    if not client:
        conn.close()
        return redirect(url_for("barista", ok="⚠️ Cliente no encontrado"))

    serial_id, client_name, current_stamps = client[0], client[1], (client[2] or 0)

    if action == "sub":
        new_stamps = max(0, current_stamps - 1)
        action_label = f"Sello restado a {client_name} ({serial_id}): {new_stamps}/10"
    elif action == "reset":
        new_stamps = 0
        action_label = f"🎁 Recompensa canjeada para {client_name} ({serial_id}): reiniciado a 0/10"
    else:
        if current_stamps >= 10:
            conn.close()
            return redirect(url_for(
                "barista",
                ok=f"ℹ️ {client_name} ({serial_id}) ya tiene 10/10 sellos. Usa '🎁 Canjear' para reiniciar su tarjeta.",
                updated=serial_id
            ))
        new_stamps = current_stamps + 1
        action_label = f"✅ Sello agregado a {client_name} ({serial_id}): ahora tiene {new_stamps}/10"

    now_ts = get_utc_timestamp()
    c.execute(
        "UPDATE clients SET stamps = ?, updated_at = ? WHERE serial = ?",
        (new_stamps, now_ts, serial_id)
    )
    conn.commit()
    conn.close()

    push_summary = send_apns_push_for_serial(serial_id)
    return redirect(url_for("barista", ok=f"{action_label} — {push_summary}", updated=serial_id))


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


# 4. Descarga del pase actualizado (el iPhone baja la versión nueva con los sellos/mensajes)
@app.route("/v1/passes/<pass_type_id>/<serial_number>", methods=["GET"])
def get_latest_pass(pass_type_id, serial_number):
    if not is_authorized():
        return "", 401
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT name, stamps, message, phone, birthdate FROM clients WHERE serial = ?",
        (serial_number,)
    )
    client = c.fetchone()
    if not client:
        conn.close()
        return "", 404

    # Registrar el momento exacto en que el iPhone descargó la actualización
    c.execute("UPDATE clients SET last_sync_at = ? WHERE serial = ?", (get_short_time(), serial_number))
    conn.commit()
    conn.close()

    name, stamps, message, phone, birthdate = client
    pkpass_path = generate_signed_pkpass(
        serial_number, name, stamps or 0, request.host_url, message, phone=phone, birthdate=birthdate
    )
    # conditional=False y Cache-Control: no-store garantizan que el iPhone siempre reciba HTTP 200 con el pase nuevo
    response = send_file(pkpass_path, mimetype="application/vnd.apple.pkpass", conditional=False, max_age=0)
    response.headers["Last-Modified"] = formatdate(timeval=None, localtime=False, usegmt=True)
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


# 6. Envío de Push a Apple APNs (a todos los iPhones registrados para ese pase)
def send_apns_push_for_serial(serial):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    reg_tokens = [r[0] for r in c.execute("SELECT push_token FROM registrations WHERE serial = ? AND push_token IS NOT NULL", (serial,)).fetchall()]
    client_row = c.execute("SELECT push_token FROM clients WHERE serial = ?", (serial,)).fetchone()
    if client_row and client_row[0] and client_row[0] not in reg_tokens:
        reg_tokens.append(client_row[0])

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
            # Si Apple reporta que el token ya no es válido (410 Unregistered), limpiarlo
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
            # Wallet usa un payload vacío: despierta de inmediato al iPhone para que descargue el pase actualizado
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
        "database": {},
        "recent_wallet_logs": []
    }
    try:
        import h2  # noqa: F401
        report["http2_support"] = True
    except ImportError as e:
        report["http2_error"] = str(e)

    # Probar firma de pase temporal
    try:
        test_path = generate_signed_pkpass("MAISHAS-DIAG01", "Prueba Diag", 1, request.host_url, "Diagnóstico OK")
        report["pass_signing"] = os.path.isfile(test_path) and os.path.getsize(test_path) > 1000
    except Exception as e:
        report["pass_signing_error"] = str(e)

    # Probar conexión TLS HTTP/2 real con Apple APNs
    try:
        ok, code, detail = send_apns_push("0000000000000000000000000000000000000000000000000000000000000000")
        # 400 BadDeviceToken confirma que Apple aceptó nuestro certificado SSL y nuestra conexión HTTP/2
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
