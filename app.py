from flask import Flask, request, jsonify, send_file, render_template_string, redirect, url_for
from werkzeug.middleware.proxy_fix import ProxyFix
import os, json, hashlib, zipfile, subprocess, urllib.request, uuid, httpx, ssl, sqlite3

app = Flask(__name__)
# Respetar los encabezados del túnel HTTPS (X-Forwarded-Proto / Host)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

from functools import wraps
from flask import Response

def check_auth(username, password):
    # Por defecto el usuario es "maishas" y la clave "cafe2026" (se puede cambiar en Railway)
    return username == 'maishas' and password == os.environ.get('BARISTA_PASSWORD', 'cafe2026')

def authenticate():
    return Response('Acceso Denegado. Ingresa las credenciales del panel.', 401, {'WWW-Authenticate': 'Basic realm="Login Panel Barista"'})

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

AUTH_TOKEN = os.environ.get("AUTH_TOKEN", "MaishasSecretAuthToken2026")
DEFAULT_MESSAGE = "¡Bienvenido a Maishas Café! Aquí verás nuestras promociones."


def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS clients (
            serial TEXT PRIMARY KEY,
            name TEXT,
            email TEXT,
            stamps INTEGER DEFAULT 0,
            push_token TEXT,
            push_token_auth TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    # Migración: columna para el último mensaje enviado por el barista
    cols = [r[1] for r in c.execute("PRAGMA table_info(clients)")]
    if "message" not in cols:
        c.execute("ALTER TABLE clients ADD COLUMN message TEXT")
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
        body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background-color: #F3EDE2; color: #2C221C; display: flex; justify-content: center; align-items: center; min-height: 100vh; margin: 0; padding: 20px; }
        .card { background: white; padding: 30px; border-radius: 20px; box-shadow: 0 10px 30px rgba(0,0,0,0.1); width: 100%; max-width: 400px; text-align: center; }
        h1 { color: #2C221C; margin-bottom: 5px; font-size: 24px; }
        p { color: #8A6646; font-size: 14px; margin-bottom: 25px; }
        input { width: 100%; padding: 14px; margin: 10px 0; border: 1px solid #DDD; border-radius: 10px; font-size: 16px; box-sizing: border-box; }
        button { width: 100%; padding: 14px; background: #2C221C; color: #F3EDE2; border: none; border-radius: 10px; font-size: 16px; font-weight: bold; cursor: pointer; margin-top: 15px; }
        button:hover { background: #4A3B30; }
        .logo { font-size: 32px; margin-bottom: 10px; }
    </style>
</head>
<body>
    <div class="card">
        <div class="logo">☕️</div>
        <h1>Maishas Café</h1>
        <p>Regístrate para obtener tu Tarjeta de Fidelización en Apple Wallet</p>
        <form action="/api/register" method="POST">
            <input type="text" name="name" placeholder="Tu Nombre y Apellido" required>
            <input type="email" name="email" placeholder="Tu Correo Electrónico" required>
            <button type="submit">Obtener Tarjeta en Apple Wallet 📲</button>
        </form>
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
        body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background-color: #2C221C; color: #F3EDE2; padding: 20px; text-align: center; }
        .card { background: #3A2E26; padding: 20px; border-radius: 16px; max-width: 600px; margin: 0 auto 20px; }
        button { padding: 10px 15px; background: #8A6646; color: white; border: none; border-radius: 8px; font-size: 14px; cursor: pointer; margin: 5px; }
        table { width: 100%; border-collapse: collapse; margin-top: 15px; }
        th, td { padding: 10px; border-bottom: 1px solid #554438; text-align: left; }
        .stamps { font-weight: bold; color: #E8C39E; }
        .badge { background: #27ae60; color: white; padding: 3px 8px; border-radius: 10px; font-size: 11px; }
        .badge-off { background: #7f8c8d; color: white; padding: 3px 8px; border-radius: 10px; font-size: 11px; }
        textarea, select { width: 100%; box-sizing: border-box; padding: 12px; margin: 6px 0; border-radius: 10px; border: 1px solid #554438; background: #2C221C; color: #F3EDE2; font-size: 15px; font-family: inherit; }
        .hint { font-size: 12px; color: #AAA; margin: 8px 0 0; }
        .toast { background: #27ae60; color: white; padding: 12px; }
        .last-msg { color: #AAA; font-size: 11px; font-style: italic; }
    </style>
</head>
<body>
    <h1>☕️ Panel Barista - Maishas Café</h1>
    {% if ok %}<div class="card toast">{{ ok }}</div>{% endif %}
    <div class="card">
        <h3>📣 Enviar mensaje a clientes</h3>
        <form action="/api/send-message" method="POST">
            <textarea name="message" maxlength="200" rows="3" placeholder="Ej: ¡Hoy 2x1 en lattes de 4 a 6 pm! ☕️" required></textarea>
            <select name="target">
                <option value="all">Todos los clientes</option>
                {% for client in clients %}
                <option value="{{ client[0] }}">{{ client[1] }} ({{ client[0] }})</option>
                {% endfor %}
            </select>
            <button type="submit">Enviar notificación 📲</button>
        </form>
        <p class="hint">Llega como notificación a quienes tengan la tarjeta en su iPhone. Si mandas el mismo texto dos veces seguidas, iOS no vuelve a avisar: cámbialo aunque sea un poco.</p>
    </div>
    <div class="card">
        <h3>Lista de Clientes Registrados</h3>
        <table>
            <thead>
                <tr>
                    <th>Cliente</th>
                    <th>Estado Wallet</th>
                    <th>Sellos</th>
                    <th>Acción</th>
                </tr>
            </thead>
            <tbody>
                {% for client in clients %}
                <tr>
                    <td>
                        <strong>{{ client[1] }}</strong><br>
                        <small style="color: #AAA;">{{ client[0] }}</small>
                        {% if client[5] %}<br><span class="last-msg">💬 {{ client[5] }}</span>{% endif %}
                    </td>
                    <td>
                        {% if client[4] %}
                        <span class="badge">📲 Instalado en iPhone</span>
                        {% else %}
                        <span class="badge-off">⏳ Esperando instalación</span>
                        {% endif %}
                    </td>
                    <td class="stamps">{{ client[3] }} / 10</td>
                    <td>
                        <form action="/api/add-stamp" method="POST" style="display:inline;">
                            <input type="hidden" name="serial" value="{{ client[0] }}">
                            <button type="submit">+1 Sello ☕️</button>
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
</body>
</html>
"""

def generate_signed_pkpass(serial_number, client_name, stamps_count, host_url, message=None):
    temp_dir = f"/tmp/pass_{serial_number}"
    os.makedirs(temp_dir, exist_ok=True)
    
    subprocess.run(f'cp -R "{PASS_TEMPLATE_DIR}"/* "{temp_dir}"/', shell=True, check=True)
    
    pass_json_path = os.path.join(temp_dir, "pass.json")
    with open(pass_json_path, "r") as f:
        pass_data = json.load(f)
        
    pass_data["serialNumber"] = serial_number
    pass_data["barcodes"][0]["message"] = serial_number
    pass_data["barcodes"][0]["altText"] = serial_number
    pass_data["storeCard"]["headerFields"][0]["value"] = f"{stamps_count} / 10"
    pass_data["storeCard"]["secondaryFields"][0]["value"] = client_name
    
    # 🔔 Configuración del Web Service para Notificaciones Push (APNs)
    # Apple Wallet usará esta URL para registrar el dispositivo y pedir actualizaciones
    clean_host = host_url.rstrip("/")
    pass_data["webServiceURL"] = f"{clean_host}/"
    pass_data["authenticationToken"] = AUTH_TOKEN
    
    # Notificación visible cuando cambia un sello
    pass_data["storeCard"]["headerFields"][0]["changeMessage"] = "¡Has ganado 1 nuevo sello en Maishas Café! ☕️ Total: %@"

    # 📣 Mensaje del barista: al cambiar este valor, iOS muestra el texto como notificación
    pass_data["storeCard"]["backFields"].insert(0, {
        "key": "news",
        "label": "Mensaje de Maishas Café",
        "value": message or DEFAULT_MESSAGE,
        "changeMessage": "%@"
    })
    
    with open(pass_json_path, "w") as f:
        json.dump(pass_data, f, indent=2)
        
    # Manifest
    manifest = {}
    for root, dirs, files in os.walk(temp_dir):
        for f in files:
            if f in ['manifest.json', 'signature', '.DS_Store']:
                continue
            full_path = os.path.join(root, f)
            rel_path = os.path.relpath(full_path, temp_dir).replace('\\', '/')
            with open(full_path, 'rb') as fp:
                manifest[rel_path] = hashlib.sha1(fp.read()).hexdigest()
                
    manifest_path = os.path.join(temp_dir, "manifest.json")
    with open(manifest_path, "w") as fp:
        json.dump(manifest, fp, indent=2)
        
    # Firmar
    sig_path = os.path.join(temp_dir, "signature")
    cmd_sign = f'openssl smime -sign -signer "{CERT_PEM}" -inkey "{KEY_PEM}" -certfile "{WWDR_PEM}" -in "{manifest_path}" -out "{sig_path}" -outform DER -binary'
    subprocess.run(cmd_sign, shell=True, check=True)
    
    output_pkpass = f"/tmp/{serial_number}.pkpass"
    with zipfile.ZipFile(output_pkpass, 'w', zipfile.ZIP_DEFLATED) as z:
        for root, dirs, files in os.walk(temp_dir):
            for f in files:
                if f == '.DS_Store':
                    continue
                full_path = os.path.join(root, f)
                rel_path = os.path.relpath(full_path, temp_dir)
                z.write(full_path, rel_path)
                
    return output_pkpass

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
    c.execute("SELECT serial, name, email, stamps, push_token, message FROM clients ORDER BY updated_at DESC")
    clients = c.fetchall()
    conn.close()
    return render_template_string(BARISTA_HTML, clients=clients, ok=request.args.get("ok"))

@app.route("/api/register", methods=["POST"])
def register():
    name = request.form.get("name")
    email = request.form.get("email")
    serial_number = f"MAISHAS-{uuid.uuid4().hex[:6].upper()}"
    
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO clients (serial, name, email, stamps, message) VALUES (?, ?, ?, 0, ?)", (serial_number, name, email, DEFAULT_MESSAGE))
    conn.commit()
    conn.close()
    
    host_url = request.host_url
    pkpass_path = generate_signed_pkpass(serial_number, name, 0, host_url, DEFAULT_MESSAGE)
    return send_file(pkpass_path, mimetype="application/vnd.apple.pkpass", as_attachment=True, download_name=f"MaishasCafe_{name.replace(' ', '_')}.pkpass")

@app.route("/api/add-stamp", methods=["POST"])
@requires_auth
def add_stamp():
    serial = request.form.get("serial")
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT serial, name, stamps, push_token FROM clients WHERE serial = ?", (serial,))
    client = c.fetchone()
    
    if client and client[2] < 10:
        new_stamps = client[2] + 1
        c.execute("UPDATE clients SET stamps = ?, updated_at = CURRENT_TIMESTAMP WHERE serial = ?", (new_stamps, serial))
        conn.commit()
        
        # Enviar notificación Push APNs si el iPhone ya registró su token
        push_token = client[3]
        if push_token:
            send_apns_push_for_serial(serial)
            
    conn.close()
    return redirect(url_for("barista", ok=f"Sello agregado a {client[1]}" if client else None))

@app.route("/api/send-message", methods=["POST"])
@requires_auth
def send_message():
    message = (request.form.get("message") or "").strip()[:200]
    target = request.form.get("target", "all")
    if not message:
        return redirect(url_for("barista", ok="Escribe un mensaje antes de enviar"))

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    if target == "all":
        c.execute("SELECT serial FROM clients")
    else:
        c.execute("SELECT serial FROM clients WHERE serial = ?", (target,))
    serials = [r[0] for r in c.fetchall()]
    for s in serials:
        c.execute("UPDATE clients SET message = ?, updated_at = CURRENT_TIMESTAMP WHERE serial = ?", (message, s))
    conn.commit()
    c.execute(f"SELECT COUNT(DISTINCT serial) FROM registrations WHERE serial IN ({','.join('?' * len(serials)) or 'NULL'})", serials)
    installed = c.fetchone()[0]
    conn.close()

    # El push "despierta" a Wallet; al descargar el pase nuevo muestra el changeMessage con el texto
    for s in serials:
        send_apns_push_for_serial(s)

    return redirect(url_for("barista", ok=f"Mensaje enviado a {installed} iPhone(s) ({len(serials)} cliente(s) actualizados)"))

# ---------------------------------------------------------
# APPLE WALLET WEB SERVICE PROTOCOL (APNs & REST API)
# Especificación: developer.apple.com/documentation/walletpasses
# ---------------------------------------------------------

def init_devices_table():
    conn = sqlite3.connect(DB_PATH)
    conn.execute('''
        CREATE TABLE IF NOT EXISTS registrations (
            device_id TEXT,
            push_token TEXT,
            serial TEXT,
            PRIMARY KEY (device_id, serial)
        )
    ''')
    conn.commit()
    conn.close()

init_devices_table()

def is_authorized():
    return request.headers.get("Authorization", "") == f"ApplePass {AUTH_TOKEN}"

# 1. Registro del dispositivo (cuando el usuario agrega la tarjeta a su iPhone)
@app.route("/v1/devices/<device_id>/registrations/<pass_type_id>/<serial_number>", methods=["POST"])
def register_device(device_id, pass_type_id, serial_number):
    if not is_authorized():
        return "", 401
    push_token = (request.get_json(silent=True) or {}).get("pushToken")

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT 1 FROM registrations WHERE device_id = ? AND serial = ?", (device_id, serial_number))
    exists = c.fetchone()
    c.execute("INSERT OR REPLACE INTO registrations (device_id, push_token, serial) VALUES (?, ?, ?)",
              (device_id, push_token, serial_number))
    c.execute("UPDATE clients SET push_token = ? WHERE serial = ?", (push_token, serial_number))
    conn.commit()
    conn.close()

    print(f"📲 iPhone registrado para el pase {serial_number}")
    return "", (200 if exists else 201)

# 2. Baja del dispositivo (cuando el usuario elimina la tarjeta)
@app.route("/v1/devices/<device_id>/registrations/<pass_type_id>/<serial_number>", methods=["DELETE"])
def unregister_device(device_id, pass_type_id, serial_number):
    if not is_authorized():
        return "", 401
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM registrations WHERE device_id = ? AND serial = ?", (device_id, serial_number))
    conn.execute("UPDATE clients SET push_token = NULL WHERE serial = ? AND serial NOT IN (SELECT serial FROM registrations)", (serial_number,))
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
        "lastUpdated": max(r[1] for r in rows)
    })

# 4. Descarga del pase actualizado (el iPhone baja la versión nueva con los sellos)
@app.route("/v1/passes/<pass_type_id>/<serial_number>", methods=["GET"])
def get_latest_pass(pass_type_id, serial_number):
    if not is_authorized():
        return "", 401
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT name, stamps, message FROM clients WHERE serial = ?", (serial_number,))
    client = c.fetchone()
    conn.close()
    if not client:
        return "", 404

    name, stamps, message = client
    pkpass_path = generate_signed_pkpass(serial_number, name, stamps, request.host_url, message)
    return send_file(pkpass_path, mimetype="application/vnd.apple.pkpass")

# 5. Log de errores que reporta el iPhone (útil para depurar)
@app.route("/v1/log", methods=["POST"])
def wallet_log():
    for line in (request.get_json(silent=True) or {}).get("logs", []):
        print(f"📝 Wallet log: {line}")
    return "", 200

# 6. Envío de Push a Apple APNs (a todos los iPhones registrados para ese pase)
def send_apns_push_for_serial(serial):
    conn = sqlite3.connect(DB_PATH)
    tokens = [r[0] for r in conn.execute("SELECT push_token FROM registrations WHERE serial = ?", (serial,)).fetchall()]
    conn.close()
    for token in tokens:
        send_apns_push(token)

def send_apns_push(push_token):
    try:
        url = f"https://api.push.apple.com/3/device/{push_token}"
        context = ssl.create_default_context()
        context.load_cert_chain(certfile=CERT_PEM, keyfile=KEY_PEM)
        with httpx.Client(http2=True, verify=context) as client:
            headers = {"apns-topic": "pass.com.maishascafe.loyalty"}
            # Wallet usa un payload vacío: solo "despierta" al iPhone para que pida el pase nuevo
            res = client.post(url, json={}, headers=headers)
            print(f"📡 Push APNs enviado. Status: {res.status_code} {res.text}")
    except Exception as e:
        print(f"⚠️ Error enviando Push APNs: {e}")

if __name__ == "__main__":
    print("🚀 Servidor con soporte APNs Push iniciado en http://localhost:5001")
    port = int(os.environ.get('PORT', 5001))
    app.run(host="0.0.0.0", port=port, debug=False)
