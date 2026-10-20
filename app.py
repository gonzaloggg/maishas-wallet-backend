from flask import Flask, request, jsonify, send_file, render_template_string, redirect, url_for, Response
from werkzeug.middleware.proxy_fix import ProxyFix
from functools import wraps
from datetime import datetime, timezone, date
from email.utils import formatdate, parsedate_to_datetime
from PIL import Image, ImageDraw
import os, json, hashlib, zipfile, subprocess, uuid, httpx, ssl, sqlite3, tempfile, shutil, math

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
DEFAULT_GEO_TEXT = "☕️ ¡Estás cerca de Maishas Café! Pasa por tu café favorito y suma tu sello de hoy ✨"

MESES_ES = ["", "Ene", "Feb", "Mar", "Abr", "May", "Jun", "Jul", "Ago", "Sep", "Oct", "Nov", "Dic"]


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
            "key": "gold",
            "name": "Socio Oro VIP ♛",
            "badge": "♛ Oro VIP",
            "bg": "linear-gradient(135deg, #D4AF37 0%, #F3E5AB 50%, #AA7C11 100%)",
            "fg": "#12100E",
            "border": "rgba(212, 175, 55, 0.5)",
            "next_goal": "¡Máximo nivel VIP alcanzado!"
        }
    elif visits >= 10:
        return {
            "key": "silver",
            "name": "Socio Plata ✦",
            "badge": "✦ Plata",
            "bg": "linear-gradient(135deg, #E0E5EC 0%, #B8C2CC 50%, #8A95A5 100%)",
            "fg": "#12100E",
            "border": "rgba(224, 229, 236, 0.4)",
            "next_goal": f"A {25 - visits} visitas de Oro VIP ♛"
        }
    else:
        return {
            "key": "bronze",
            "name": "Amante del Café ☕️",
            "badge": "🌱 Bronce",
            "bg": "rgba(198, 166, 117, 0.16)",
            "fg": "#E6C280",
            "border": "rgba(198, 166, 117, 0.3)",
            "next_goal": f"A {10 - visits} visitas de Nivel Plata ✦"
        }


def calculate_birthday_info(birthdate_str, ref_date=None):
    """
    Analiza la fecha de nacimiento (YYYY-MM-DD) e identifica si el cliente cumple años
    HOY, en los próximos 7 días o en el mes actual (manejando cambios de año y años bisiestos).
    """
    empty_res = {
        "has_birthdate": False,
        "is_today": False,
        "is_upcoming_week": False,
        "is_this_month": False,
        "days_until": 999,
        "short_date": "",
        "badge_text": "",
    }
    if not birthdate_str:
        return empty_res

    raw = birthdate_str.strip()
    try:
        parts = raw.replace("/", "-").split("-")
        if len(parts) == 3:
            if len(parts[0]) == 4:
                month, day = int(parts[1]), int(parts[2])
            else:
                day, month = int(parts[0]), int(parts[1])
        elif len(parts) == 2:
            month, day = int(parts[0]), int(parts[1])
        else:
            return empty_res

        if not (1 <= month <= 12 and 1 <= day <= 31):
            return empty_res
    except Exception:
        return empty_res

    today = ref_date or datetime.now().date()

    def _safe_date(yr, m, d):
        try:
            return date(yr, m, d)
        except ValueError:
            # Manejo de 29 de febrero en año no bisiesto -> 28 de febrero
            return date(yr, 2, 28)

    bday_this_year = _safe_date(today.year, month, day)
    if bday_this_year < today:
        next_bday = _safe_date(today.year + 1, month, day)
    else:
        next_bday = bday_this_year

    days_until = (next_bday - today).days
    short_date = f"{day:02d} {MESES_ES[month]}"
    is_today = (days_until == 0)
    is_upcoming_week = (0 <= days_until <= 7)
    is_this_month = (month == today.month)

    if is_today:
        badge_text = "🎉 ¡HOY!"
    elif days_until == 1:
        badge_text = f"Mañana · {short_date}"
    elif is_upcoming_week:
        badge_text = f"En {days_until} días · {short_date}"
    else:
        badge_text = f"🎂 {short_date}"

    return {
        "has_birthdate": True,
        "is_today": is_today,
        "is_upcoming_week": is_upcoming_week,
        "is_this_month": is_this_month,
        "days_until": days_until,
        "short_date": short_date,
        "badge_text": badge_text,
    }


def render_dynamic_strip(stamps_count, output_dir, total_visits=0):
    """
    Genera dinámicamente strip.png (375x125) y strip@2x.png (750x250) con Pillow (PIL)
    usando super-sampling 2x (1500x500) y filtro LANCZOS para bordes perfectamente suaves.
    Dibuja una matriz Luxury Coffee de 10 sellos (2 filas de 5 nodos).
    """
    stamps = max(0, min(10, int(stamps_count or 0)))
    W, H = 1500, 500
    img = Image.new("RGB", (W, H), (18, 16, 14))
    draw = ImageDraw.Draw(img)

    # 1. Fondo degradado cálido Espresso oscuro
    for y in range(H):
        t = y / float(H)
        # Degradado sutil vertical con centro ligeramente más cálido
        mid = 1.0 - abs(t - 0.5) * 2.0
        if stamps >= 10:
            # Estado celebración 10/10: resplandor cálido dorado en el fondo
            r = int(20 + 28 * mid)
            g = int(17 + 22 * mid)
            b = int(13 + 12 * mid)
        else:
            r = int(16 + 14 * mid)
            g = int(14 + 11 * mid)
            b = int(12 + 9 * mid)
        draw.line([(0, y), (W, y)], fill=(r, g, b))

    # 2. Marco perimetral estilo Luxury Card
    border_col = (212, 175, 55) if stamps >= 10 else (82, 68, 50)
    inner_border_col = (145, 118, 68) if stamps >= 10 else (50, 42, 33)
    draw.rounded_rectangle([24, 20, W - 25, H - 21], radius=36, outline=border_col, width=4)
    draw.rounded_rectangle([36, 32, W - 37, H - 33], radius=28, outline=inner_border_col, width=2)

    # 3. Matriz de 10 Sellos (2 filas x 5 columnas)
    cols_x = [230, 490, 750, 1010, 1270]
    rows_y = [152, 348]
    radius = 68

    for idx in range(10):
        row = idx // 5
        col = idx % 5
        cx = cols_x[col]
        cy = rows_y[row]
        is_earned = (idx < stamps)
        is_tenth = (idx == 9)

        if is_earned:
            # Halo exterior dorado suave
            halo_r = radius + 12
            draw.ellipse(
                [cx - halo_r, cy - halo_r, cx + halo_r, cy + halo_r],
                fill=(48, 39, 24),
                outline=(110, 88, 44),
                width=2
            )
            # Círculo principal Dorado Champagne (#D4AF37)
            fill_gold = (232, 196, 96) if stamps >= 10 else (212, 175, 55)
            rim_gold = (248, 232, 176)
            draw.ellipse(
                [cx - radius, cy - radius, cx + radius, cy + radius],
                fill=fill_gold,
                outline=rim_gold,
                width=5
            )
            # Anillo interior oscuro sutil
            in_r = radius - 14
            draw.ellipse(
                [cx - in_r, cy - in_r, cx + in_r, cy + in_r],
                outline=(150, 118, 30),
                width=3
            )

            if is_tenth:
                # En el sello #10 completado dibujamos una estrella/corona de premio
                pts = []
                for i in range(10):
                    ang = -math.pi / 2 + i * (math.pi / 5)
                    rad_p = 30 if i % 2 == 0 else 13
                    pts.append((cx + rad_p * math.cos(ang), cy + rad_p * math.sin(ang)))
                draw.polygon(pts, fill=(22, 18, 15))
            else:
                # Ícono minimalista de grano de café / taza en el centro
                bean_rx, bean_ry = 20, 27
                draw.ellipse(
                    [cx - bean_rx, cy - bean_ry, cx + bean_rx, cy + bean_ry],
                    fill=(24, 20, 16)
                )
                # Línea central del grano de café
                draw.arc(
                    [cx - 14, cy - bean_ry + 3, cx + 18, cy + bean_ry - 3],
                    start=110, end=250, fill=fill_gold, width=5
                )
        else:
            # Sello pendiente: anillo translúcido elegante sobre fondo espresso
            bg_pending = (22, 19, 16)
            ring_color = (158, 130, 84) if is_tenth else (76, 64, 50)
            ring_width = 6 if is_tenth else 4
            draw.ellipse(
                [cx - radius, cy - radius, cx + radius, cy + radius],
                fill=bg_pending,
                outline=ring_color,
                width=ring_width
            )
            in_r = radius - 15
            draw.ellipse(
                [cx - in_r, cy - in_r, cx + in_r, cy + in_r],
                outline=(52, 44, 35) if not is_tenth else (110, 90, 58),
                width=2
            )

            if is_tenth:
                # Estrella sutil en el nodo #10 indicando la recompensa final
                pts = []
                for i in range(10):
                    ang = -math.pi / 2 + i * (math.pi / 5)
                    rad_p = 24 if i % 2 == 0 else 10
                    pts.append((cx + rad_p * math.cos(ang), cy + rad_p * math.sin(ang)))
                draw.polygon(pts, outline=(158, 130, 84), fill=(42, 35, 26))
            else:
                # Punto central minimalista
                dot_r = 8
                draw.ellipse(
                    [cx - dot_r, cy - dot_r, cx + dot_r, cy + dot_r],
                    fill=(66, 56, 44)
                )

    # 4. Guardar strip@2x.png (750x250) y strip.png (375x125) con suavizado LANCZOS
    img_2x = img.resize((750, 250), Image.Resampling.LANCZOS)
    img_1x = img.resize((375, 125), Image.Resampling.LANCZOS)

    img_2x.save(os.path.join(output_dir, "strip@2x.png"), format="PNG", optimize=True)
    img_1x.save(os.path.join(output_dir, "strip.png"), format="PNG", optimize=True)


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


# ---------------------------------------------------------
# PLANTILLA 1: PORTAL PÚBLICO DE REGISTRO (APPLE HIG LUXURY)
# ---------------------------------------------------------
REGISTRATION_HTML = """
<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>Maishas Café — Club de Fidelización Apple Wallet</title>
    <style>
        :root {
            --bg-deep: #0D0C0B;
            --bg-surface: rgba(26, 23, 20, 0.78);
            --gold: #D4AF37;
            --gold-light: #E6C280;
            --text-primary: #F5F2EB;
            --text-muted: #A39B90;
            --border-glass: rgba(255, 255, 255, 0.09);
            --rose: #FF375F;
        }
        * { box-sizing: border-box; -webkit-font-smoothing: antialiased; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, "SF Pro Display", "SF Pro Text", "Helvetica Neue", sans-serif;
            background-color: var(--bg-deep);
            background-image:
                radial-gradient(circle at 50% 0%, rgba(212, 175, 55, 0.16) 0%, transparent 55%),
                radial-gradient(circle at 85% 90%, rgba(198, 166, 117, 0.08) 0%, transparent 45%);
            color: var(--text-primary);
            min-height: 100vh;
            margin: 0;
            padding: 24px 16px 40px;
            display: flex;
            justify-content: center;
            align-items: center;
        }
        .layout-wrapper {
            width: 100%;
            max-width: 440px;
            display: flex;
            flex-direction: column;
            gap: 20px;
        }
        .brand-header {
            text-align: center;
        }
        .brand-pill {
            display: inline-flex;
            align-items: center;
            gap: 6px;
            padding: 5px 12px;
            border-radius: 999px;
            background: rgba(212, 175, 55, 0.12);
            border: 1px solid rgba(212, 175, 55, 0.28);
            color: var(--gold-light);
            font-size: 11px;
            font-weight: 600;
            letter-spacing: 0.6px;
            text-transform: uppercase;
            margin-bottom: 10px;
        }
        .brand-header h1 {
            margin: 0;
            font-size: 28px;
            font-weight: 700;
            letter-spacing: -0.5px;
            color: var(--text-primary);
        }
        .brand-header p {
            margin: 6px 0 0;
            font-size: 14px;
            color: var(--text-muted);
        }

        /* Previsualización Interactiva de la Tarjeta Apple Wallet */
        .wallet-preview {
            background: linear-gradient(165deg, #1A1714 0%, #110F0D 100%);
            border-radius: 20px;
            border: 1px solid rgba(212, 175, 55, 0.32);
            box-shadow: 0 20px 45px rgba(0, 0, 0, 0.65), inset 0 1px 0 rgba(255, 255, 255, 0.12);
            overflow: hidden;
            transition: transform 0.3s cubic-bezier(0.16, 1, 0.3, 1);
        }
        .wp-header {
            padding: 14px 18px;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        .wp-logo {
            display: flex;
            align-items: center;
            gap: 8px;
            font-weight: 700;
            font-size: 13px;
            letter-spacing: 0.8px;
            color: var(--text-primary);
        }
        .wp-logo-icon {
            width: 24px;
            height: 24px;
            border-radius: 7px;
            background: linear-gradient(135deg, #D4AF37, #9A7B2C);
            color: #12100E;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 13px;
        }
        .wp-stamps-top {
            text-align: right;
            font-variant-numeric: tabular-nums;
        }
        .wp-stamps-top .lbl {
            display: block;
            font-size: 9px;
            font-weight: 700;
            letter-spacing: 0.8px;
            color: #C6A675;
        }
        .wp-stamps-top .val {
            font-size: 15px;
            font-weight: 700;
            color: var(--text-primary);
        }
        /* Strip visual con los 10 sellos */
        .wp-strip {
            background: radial-gradient(circle at 50% 50%, #241E18 0%, #12100E 100%);
            border-top: 1px solid rgba(198, 166, 117, 0.2);
            border-bottom: 1px solid rgba(198, 166, 117, 0.2);
            padding: 14px 22px;
            display: grid;
            grid-template-columns: repeat(5, 1fr);
            gap: 10px;
            justify-items: center;
        }
        .wp-node {
            width: 32px;
            height: 32px;
            border-radius: 50%;
            border: 1.5px solid rgba(198, 166, 117, 0.35);
            background: rgba(255, 255, 255, 0.02);
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 11px;
            color: rgba(198, 166, 117, 0.55);
        }
        .wp-node.active {
            background: linear-gradient(135deg, #E6C280, #D4AF37);
            border-color: #FFF2C2;
            color: #12100E;
            box-shadow: 0 0 12px rgba(212, 175, 55, 0.45);
            font-weight: 800;
        }
        .wp-node.prize {
            border-color: rgba(212, 175, 55, 0.75);
            color: var(--gold-light);
        }
        .wp-fields {
            padding: 12px 18px 14px;
            display: grid;
            grid-template-columns: 1.3fr 1fr 1fr;
            gap: 8px;
        }
        .wp-field .lbl {
            font-size: 9px;
            font-weight: 700;
            color: #C6A675;
            letter-spacing: 0.6px;
            text-transform: uppercase;
            display: block;
            margin-bottom: 2px;
        }
        .wp-field .val {
            font-size: 12px;
            font-weight: 600;
            color: var(--text-primary);
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
            display: block;
        }

        /* Tarjeta Glassmorphic del Formulario */
        .form-card {
            background: var(--bg-surface);
            backdrop-filter: blur(24px);
            -webkit-backdrop-filter: blur(24px);
            border: 1px solid var(--border-glass);
            border-radius: 24px;
            padding: 26px 22px;
            box-shadow: 0 16px 40px rgba(0, 0, 0, 0.45);
        }
        .field-group {
            margin-bottom: 15px;
            text-align: left;
        }
        .field-label {
            display: flex;
            justify-content: space-between;
            align-items: center;
            font-size: 13px;
            font-weight: 600;
            color: #E5E0D8;
            margin-bottom: 6px;
        }
        .optional-tag {
            font-size: 11px;
            font-weight: 500;
            color: var(--text-muted);
            background: rgba(255, 255, 255, 0.06);
            padding: 2px 8px;
            border-radius: 99px;
        }
        input {
            width: 100%;
            padding: 13px 14px;
            border-radius: 12px;
            border: 1px solid rgba(255, 255, 255, 0.12);
            background: rgba(13, 12, 11, 0.65);
            color: var(--text-primary);
            font-size: 16px;
            font-family: inherit;
            transition: all 0.2s ease;
        }
        input::placeholder { color: rgba(163, 155, 144, 0.55); }
        input:focus {
            outline: none;
            border-color: var(--gold);
            box-shadow: 0 0 0 3px rgba(212, 175, 55, 0.2);
            background: rgba(18, 16, 14, 0.95);
        }
        input[type="date"] {
            color-scheme: dark;
        }
        .bday-incentive {
            margin-top: 6px;
            display: flex;
            align-items: center;
            gap: 6px;
            font-size: 12px;
            color: #FFB3C1;
            background: rgba(255, 55, 95, 0.12);
            border: 1px solid rgba(255, 55, 95, 0.25);
            padding: 7px 11px;
            border-radius: 10px;
        }
        .btn-apple-wallet {
            width: 100%;
            margin-top: 8px;
            padding: 15px 18px;
            border-radius: 14px;
            border: 1px solid rgba(255, 255, 255, 0.18);
            background: linear-gradient(135deg, #D4AF37 0%, #B58E24 100%);
            color: #12100E;
            font-size: 16px;
            font-weight: 700;
            letter-spacing: -0.2px;
            cursor: pointer;
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 8px;
            box-shadow: 0 8px 24px rgba(212, 175, 55, 0.3);
            transition: transform 0.15s ease, box-shadow 0.15s ease;
        }
        .btn-apple-wallet:active {
            transform: scale(0.98);
        }
        .safari-hint {
            text-align: center;
            font-size: 12px;
            color: var(--text-muted);
            margin-top: 14px;
            margin-bottom: 0;
        }
    </style>
</head>
<body>
    <div class="layout-wrapper">
        <div class="brand-header">
            <span class="brand-pill">☕️ Maishas Loyalty Club</span>
            <h1>Maishas Café</h1>
            <p>Tu membresía digital interactiva en Apple Wallet</p>
        </div>

        <!-- Previsualización en Vivo estilo Apple Wallet -->
        <div class="wallet-preview" aria-label="Vista previa de tu tarjeta">
            <div class="wp-header">
                <div class="wp-logo">
                    <span class="wp-logo-icon">☕️</span>
                    <span>MAISHAS CAFÉ</span>
                </div>
                <div class="wp-stamps-top">
                    <span class="lbl">SELLOS</span>
                    <span class="val">1 / 10</span>
                </div>
            </div>
            <div class="wp-strip">
                <div class="wp-node active">☕️</div>
                <div class="wp-node">•</div>
                <div class="wp-node">•</div>
                <div class="wp-node">•</div>
                <div class="wp-node">•</div>
                <div class="wp-node">•</div>
                <div class="wp-node">•</div>
                <div class="wp-node">•</div>
                <div class="wp-node">•</div>
                <div class="wp-node prize">★</div>
            </div>
            <div class="wp-fields">
                <div class="wp-field">
                    <span class="lbl">CLIENTE</span>
                    <span class="val" id="preview-name">Tu Nombre Aquí</span>
                </div>
                <div class="wp-field">
                    <span class="lbl">NIVEL VIP</span>
                    <span class="val">Amante del Café ☕️</span>
                </div>
                <div class="wp-field" style="text-align:right;">
                    <span class="lbl">PREMIO</span>
                    <span class="val">1 Café Gratis</span>
                </div>
            </div>
        </div>

        <!-- Formulario de Registro -->
        <div class="form-card">
            <form action="/api/register" method="POST">
                <div class="field-group">
                    <label class="field-label">
                        <span>Nombre y Apellido</span>
                    </label>
                    <input type="text" name="name" id="name-input" placeholder="Ej. Ana Sofía López" required oninput="updateCardPreview(this.value)">
                </div>

                <div class="field-group">
                    <label class="field-label">
                        <span>Correo Electrónico</span>
                    </label>
                    <input type="email" name="email" placeholder="tu@correo.com" required>
                </div>

                <div class="field-group">
                    <label class="field-label">
                        <span>Número de Celular</span>
                        <span class="optional-tag">Opcional</span>
                    </label>
                    <input type="tel" name="phone" placeholder="Ej. 667 123 4567">
                </div>

                <div class="field-group">
                    <label class="field-label">
                        <span>🎂 Fecha de Nacimiento</span>
                        <span class="optional-tag">Opcional</span>
                    </label>
                    <input type="date" name="birthdate">
                    <div class="bday-incentive">
                        <span>🎁</span>
                        <span>Recibe un regalo especial en tu Apple Wallet el día de tu cumpleaños</span>
                    </div>
                </div>

                <button type="submit" class="btn-apple-wallet">
                    <span> Agregar a Apple Wallet</span>
                </button>
            </form>
            <p class="safari-hint">Ábrelo desde <strong>Safari en tu iPhone</strong> para instalar tu pase con un toque.</p>
        </div>
    </div>

    <script>
        function updateCardPreview(val) {
            const el = document.getElementById('preview-name');
            const clean = (val || '').trim();
            el.textContent = clean ? clean : 'Tu Nombre Aquí';
        }
    </script>
</body>
</html>
"""


# ---------------------------------------------------------
# PLANTILLA 2: PANEL DEL BARISTA (APPLE HIG POS — iPadOS / macOS Sequoia)
# ---------------------------------------------------------
BARISTA_HTML = """
<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Maishas Café — POS & Loyalty Console</title>
    <script src="https://cdn.jsdelivr.net/npm/jsqr@1.4.0/dist/jsQR.min.js"></script>
    <style>
        :root {
            --bg-canvas: #0D0C0B;
            --bg-elevated: #161412;
            --bg-glass: rgba(24, 21, 18, 0.75);
            --bg-glass-subtle: rgba(255, 255, 255, 0.035);
            --border-subtle: rgba(255, 255, 255, 0.08);
            --border-gold: rgba(212, 175, 55, 0.35);
            --gold: #D4AF37;
            --gold-light: #E6C280;
            --apple-green: #30D158;
            --apple-rose: #FF375F;
            --apple-amber: #FF9F0A;
            --text-primary: #F5F2EB;
            --text-secondary: #A8A096;
            --text-tertiary: #6E675F;
        }
        * { box-sizing: border-box; -webkit-font-smoothing: antialiased; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, "SF Pro Display", "SF Pro Text", "Helvetica Neue", sans-serif;
            font-variant-numeric: tabular-nums;
            background-color: var(--bg-canvas);
            background-image:
                radial-gradient(circle at 15% 0%, rgba(212, 175, 55, 0.09) 0%, transparent 45%),
                radial-gradient(circle at 85% 15%, rgba(255, 55, 95, 0.05) 0%, transparent 40%);
            color: var(--text-primary);
            margin: 0;
            padding: 0 0 60px;
            min-height: 100vh;
        }

        /* Dynamic Island Toast Notification */
        .dynamic-island-wrap {
            position: fixed;
            top: 14px;
            left: 50%;
            transform: translateX(-50%);
            z-index: 9999;
            width: 92%;
            max-width: 620px;
            pointer-events: none;
        }
        .dynamic-island {
            pointer-events: auto;
            background: rgba(18, 16, 14, 0.92);
            backdrop-filter: blur(28px);
            -webkit-backdrop-filter: blur(28px);
            border: 1px solid rgba(48, 209, 88, 0.45);
            box-shadow: 0 18px 45px rgba(0, 0, 0, 0.75), 0 0 20px rgba(48, 209, 88, 0.15);
            color: var(--text-primary);
            padding: 12px 18px;
            border-radius: 999px;
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 12px;
            font-size: 13.5px;
            font-weight: 600;
            animation: islandDrop 0.4s cubic-bezier(0.16, 1, 0.3, 1);
        }
        @keyframes islandDrop {
            0% { opacity: 0; transform: translateY(-24px) scale(0.92); }
            100% { opacity: 1; transform: translateY(0) scale(1); }
        }
        .di-dot {
            width: 10px;
            height: 10px;
            border-radius: 50%;
            background: var(--apple-green);
            box-shadow: 0 0 10px var(--apple-green);
            flex-shrink: 0;
        }
        .di-close {
            background: rgba(255, 255, 255, 0.08);
            border: none;
            color: var(--text-secondary);
            width: 24px;
            height: 24px;
            border-radius: 50%;
            cursor: pointer;
            font-size: 12px;
            display: flex;
            align-items: center;
            justify-content: center;
            flex-shrink: 0;
        }

        /* Sticky Glass Header */
        .sticky-header {
            position: sticky;
            top: 0;
            z-index: 100;
            background: rgba(13, 12, 11, 0.78);
            backdrop-filter: blur(24px);
            -webkit-backdrop-filter: blur(24px);
            border-bottom: 1px solid var(--border-subtle);
            padding: 12px 24px;
        }
        .header-inner {
            max-width: 1280px;
            margin: 0 auto;
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 16px;
            flex-wrap: wrap;
        }
        .brand-Cluster {
            display: flex;
            align-items: center;
            gap: 12px;
        }
        .brand-emblem {
            width: 38px;
            height: 38px;
            border-radius: 11px;
            background: linear-gradient(135deg, #D4AF37 0%, #8F6B1A 100%);
            color: #12100E;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 20px;
            box-shadow: 0 4px 14px rgba(212, 175, 55, 0.25);
        }
        .brand-title {
            font-size: 18px;
            font-weight: 700;
            letter-spacing: -0.3px;
            margin: 0;
        }
        .brand-sub {
            font-size: 11px;
            color: var(--gold-light);
            font-weight: 600;
            letter-spacing: 0.4px;
            text-transform: uppercase;
        }

        /* KPI Pills in Header */
        .kpi-cluster {
            display: flex;
            align-items: center;
            gap: 8px;
            flex-wrap: wrap;
        }
        .kpi-pill {
            background: var(--bg-glass-subtle);
            border: 1px solid var(--border-subtle);
            padding: 6px 12px;
            border-radius: 999px;
            font-size: 12px;
            color: var(--text-secondary);
            display: inline-flex;
            align-items: center;
            gap: 6px;
        }
        .kpi-pill strong {
            color: var(--text-primary);
            font-size: 13.5px;
            font-weight: 700;
        }
        .kpi-pill.kpi-bday {
            border-color: rgba(255, 55, 95, 0.35);
            background: rgba(255, 55, 95, 0.1);
            color: #FFB3C1;
            cursor: pointer;
        }
        .kpi-pill.kpi-bday strong { color: #FF375F; }

        /* Spotlight Search in Header */
        .spotlight-wrap {
            position: relative;
            min-width: 260px;
            flex: 1;
            max-width: 340px;
        }
        .spotlight-input {
            width: 100%;
            padding: 9px 52px 9px 34px;
            border-radius: 10px;
            border: 1px solid var(--border-subtle);
            background: rgba(255, 255, 255, 0.05);
            color: var(--text-primary);
            font-size: 13.5px;
            font-family: inherit;
            transition: all 0.2s ease;
        }
        .spotlight-input:focus {
            outline: none;
            border-color: var(--gold);
            background: rgba(18, 16, 14, 0.95);
            box-shadow: 0 0 0 3px rgba(212, 175, 55, 0.18);
        }
        .spotlight-icon {
            position: absolute;
            left: 11px;
            top: 50%;
            transform: translateY(-50%);
            font-size: 13px;
            color: var(--text-secondary);
            pointer-events: none;
        }
        .spotlight-kbd {
            position: absolute;
            right: 10px;
            top: 50%;
            transform: translateY(-50%);
            font-size: 10px;
            font-weight: 600;
            color: var(--text-tertiary);
            background: rgba(255, 255, 255, 0.06);
            border: 1px solid rgba(255, 255, 255, 0.08);
            border-radius: 5px;
            padding: 2px 6px;
            pointer-events: none;
        }

        /* Main Container & Bento Grid */
        .main-container {
            max-width: 1280px;
            margin: 22px auto 0;
            padding: 0 24px;
        }
        .bento-grid {
            display: grid;
            grid-template-columns: 1.05fr 0.95fr 1.15fr;
            gap: 18px;
            margin-bottom: 22px;
        }
        @media (max-width: 1100px) {
            .bento-grid { grid-template-columns: 1fr 1fr; }
        }
        @media (max-width: 760px) {
            .bento-grid { grid-template-columns: 1fr; }
            .sticky-header { padding: 12px 16px; }
            .main-container { padding: 0 14px; }
        }

        .bento-card {
            background: var(--bg-glass);
            backdrop-filter: blur(24px);
            -webkit-backdrop-filter: blur(24px);
            border: 1px solid var(--border-subtle);
            border-radius: 22px;
            padding: 20px;
            box-shadow: 0 12px 32px rgba(0, 0, 0, 0.35), inset 0 1px 0 rgba(255, 255, 255, 0.06);
            display: flex;
            flex-direction: column;
            justify-content: space-between;
        }
        .bento-header {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 10px;
            margin-bottom: 12px;
        }
        .bento-title {
            font-size: 15px;
            font-weight: 700;
            letter-spacing: -0.2px;
            margin: 0;
            display: flex;
            align-items: center;
            gap: 8px;
        }
        .bento-subtitle {
            font-size: 12px;
            color: var(--text-secondary);
            margin: 0 0 12px;
            line-height: 1.4;
        }

        /* iOS Segmented Control */
        .segmented-control {
            display: inline-flex;
            background: rgba(0, 0, 0, 0.45);
            border: 1px solid var(--border-subtle);
            border-radius: 10px;
            padding: 3px;
            gap: 2px;
        }
        .seg-btn {
            background: transparent;
            border: none;
            color: var(--text-secondary);
            font-size: 11.5px;
            font-weight: 600;
            padding: 5px 10px;
            border-radius: 7px;
            cursor: pointer;
            transition: all 0.18s ease;
        }
        .seg-btn.active {
            background: rgba(255, 255, 255, 0.12);
            color: var(--text-primary);
            box-shadow: 0 2px 6px rgba(0, 0, 0, 0.3);
        }

        /* Módulo 1: Escáner QR con HUD Cámara iOS */
        .qr-hud-wrapper {
            position: relative;
            background: #080706;
            border-radius: 18px;
            border: 1px solid rgba(212, 175, 55, 0.25);
            overflow: hidden;
            min-height: 155px;
            display: flex;
             flex-direction: column;
            align-items: center;
            justify-content: center;
            margin-bottom: 12px;
        }
        #qr-video {
            width: 100%;
            max-height: 220px;
            object-fit: cover;
            display: none;
        }
        .qr-idle-placeholder {
            padding: 24px 16px;
            text-align: center;
        }
        .qr-reticle {
            width: 120px;
            height: 120px;
            position: absolute;
            top: 50%;
            left: 50%;
            transform: translate(-50%, -50%);
            border-radius: 20px;
            box-shadow: 0 0 0 999px rgba(0, 0, 0, 0.38);
            border: 2px solid rgba(212, 175, 55, 0.75);
            pointer-events: none;
            display: none;
            animation: reticlePulse 2s infinite ease-in-out;
        }
        @keyframes reticlePulse {
            0%, 100% { transform: translate(-50%, -50%) scale(1); border-color: rgba(212, 175, 55, 0.85); }
            50% { transform: translate(-50%, -50%) scale(1.05); border-color: #30D158; }
        }
        .scan-flash {
            position: absolute;
            inset: 0;
            background: rgba(48, 209, 88, 0.35);
            opacity: 0;
            pointer-events: none;
            transition: opacity 0.25s ease;
        }
        .btn-cam-launch {
            background: linear-gradient(135deg, #D4AF37 0%, #B38A22 100%);
            color: #12100E;
            border: none;
            padding: 10px 18px;
            border-radius: 12px;
            font-size: 13px;
            font-weight: 700;
            cursor: pointer;
            display: inline-flex;
            align-items: center;
            gap: 6px;
            box-shadow: 0 6px 18px rgba(212, 175, 55, 0.25);
        }
        .quick-scan-form {
            display: flex;
            gap: 8px;
        }
        .input-dark {
            width: 100%;
            padding: 10px 12px;
            border-radius: 11px;
            border: 1px solid var(--border-subtle);
            background: rgba(10, 9, 8, 0.7);
            color: var(--text-primary);
            font-size: 13px;
            font-family: inherit;
        }
        .input-dark:focus {
            outline: none;
            border-color: var(--gold);
        }
        .btn-gold-sm {
            background: rgba(212, 175, 55, 0.18);
            border: 1px solid rgba(212, 175, 55, 0.45);
            color: var(--gold-light);
            border-radius: 11px;
            padding: 10px 14px;
            font-size: 13px;
            font-weight: 700;
            cursor: pointer;
            white-space: nowrap;
        }
        .btn-gold-sm:hover {
            background: var(--gold);
            color: #12100E;
        }

        /* Módulo 2: 🎂 Radar de Cumpleaños Widget */
        .bday-card-widget {
            border-color: rgba(255, 55, 95, 0.22);
            background: linear-gradient(160deg, rgba(32, 20, 24, 0.78) 0%, rgba(22, 20, 18, 0.82) 100%);
        }
        .bday-list {
            display: flex;
            flex-direction: column;
            gap: 8px;
            max-height: 215px;
            overflow-y: auto;
            padding-right: 2px;
        }
        .bday-item {
            background: rgba(255, 255, 255, 0.035);
            border: 1px solid rgba(255, 255, 255, 0.07);
            border-radius: 14px;
            padding: 10px 12px;
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 10px;
        }
        .bday-item.is-today {
            border-color: rgba(255, 55, 95, 0.45);
            background: rgba(255, 55, 95, 0.09);
        }
        .bday-badge-today {
            background: var(--apple-rose);
            color: #FFF;
            font-size: 10.5px;
            font-weight: 800;
            padding: 3px 8px;
            border-radius: 99px;
            display: inline-block;
            animation: pulseRose 1.8s infinite;
        }
        @keyframes pulseRose {
            0%, 100% { box-shadow: 0 0 0 0 rgba(255, 55, 95, 0.55); }
            50% { box-shadow: 0 0 0 6px rgba(255, 55, 95, 0); }
        }
        .bday-badge-soon {
            background: rgba(255, 159, 10, 0.16);
            border: 1px solid rgba(255, 159, 10, 0.35);
            color: #FFB340;
            font-size: 10.5px;
            font-weight: 700;
            padding: 2px 8px;
            border-radius: 99px;
            display: inline-block;
        }
        .btn-bday-gift {
            background: linear-gradient(135deg, #FF375F 0%, #D91E47 100%);
            color: #FFF;
            border: none;
            border-radius: 10px;
            padding: 7px 11px;
            font-size: 11.5px;
            font-weight: 700;
            cursor: pointer;
            white-space: nowrap;
            box-shadow: 0 4px 12px rgba(255, 55, 95, 0.3);
        }
        .btn-bday-gift:hover { opacity: 0.92; }

        /* Módulo 3: Marketing Push & Lock Screen Preview */
        .chip-row {
            display: flex;
            gap: 6px;
            flex-wrap: wrap;
            margin-bottom: 8px;
        }
        .action-chip {
            background: rgba(255, 255, 255, 0.05);
            border: 1px solid var(--border-subtle);
            color: var(--text-secondary);
            border-radius: 999px;
            padding: 5px 10px;
            font-size: 11px;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.15s ease;
        }
        .action-chip:hover {
            background: rgba(212, 175, 55, 0.15);
            border-color: var(--border-gold);
            color: var(--gold-light);
        }
        .ios-lockscreen-preview {
            background: rgba(255, 255, 255, 0.07);
            backdrop-filter: blur(16px);
            border: 1px solid rgba(255, 255, 255, 0.12);
            border-radius: 14px;
            padding: 9px 12px;
            margin: 8px 0;
            display: flex;
            align-items: center;
            gap: 10px;
        }
        .ios-notif-icon {
            width: 32px;
            height: 32px;
            border-radius: 8px;
            background: linear-gradient(135deg, #241D17, #12100E);
            border: 1px solid var(--gold);
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 15px;
            flex-shrink: 0;
        }
        .ios-notif-body {
            flex: 1;
            min-width: 0;
        }
        .ios-notif-top {
            display: flex;
            justify-content: space-between;
            font-size: 10px;
            font-weight: 700;
            color: var(--text-secondary);
            letter-spacing: 0.3px;
        }
        .ios-notif-text {
            font-size: 12px;
            color: var(--text-primary);
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
            margin-top: 1px;
        }

        /* Lista de Clientes Estilo Apple Settings */
        .clients-section {
            background: var(--bg-glass);
            backdrop-filter: blur(24px);
            -webkit-backdrop-filter: blur(24px);
            border: 1px solid var(--border-subtle);
            border-radius: 24px;
            padding: 22px;
            box-shadow: 0 14px 40px rgba(0, 0, 0, 0.4);
        }
        .clients-top-bar {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 12px;
            flex-wrap: wrap;
            margin-bottom: 16px;
        }
        .filter-tabs {
            display: flex;
            gap: 6px;
            flex-wrap: wrap;
        }
        .filter-pill {
            background: rgba(255, 255, 255, 0.04);
            border: 1px solid var(--border-subtle);
            color: var(--text-secondary);
            padding: 7px 13px;
            border-radius: 999px;
            font-size: 12.5px;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.15s ease;
        }
        .filter-pill.active {
            background: var(--gold);
            border-color: var(--gold);
            color: #12100E;
            font-weight: 700;
        }

        .client-list {
            display: flex;
            flex-direction: column;
            gap: 10px;
        }
        .client-card-row {
            background: rgba(255, 255, 255, 0.025);
            border: 1px solid var(--border-subtle);
            border-radius: 16px;
            padding: 14px 16px;
            display: grid;
            grid-template-columns: 1.4fr 1.1fr 0.9fr auto;
            align-items: center;
            gap: 16px;
            transition: background 0.2s ease, border-color 0.2s ease;
        }
        .client-card-row:hover {
            background: rgba(255, 255, 255, 0.045);
            border-color: rgba(255, 255, 255, 0.14);
        }
        .client-card-row.highlight-row {
            background: rgba(48, 209, 88, 0.11);
            border-color: rgba(48, 209, 88, 0.45);
        }
        @media (max-width: 960px) {
            .client-card-row {
                grid-template-columns: 1fr;
                gap: 12px;
            }
        }

        .client-identity {
            display: flex;
            align-items: flex-start;
            gap: 12px;
            min-width: 0;
        }
        .avatar-circle {
            width: 42px;
            height: 42px;
            border-radius: 13px;
            background: rgba(212, 175, 55, 0.14);
            border: 1px solid rgba(212, 175, 55, 0.35);
            color: var(--gold-light);
            font-weight: 700;
            font-size: 15px;
            display: flex;
            align-items: center;
            justify-content: center;
            flex-shrink: 0;
        }
        .avatar-circle.tier-gold {
            background: linear-gradient(135deg, #D4AF37, #8F6B1A);
            color: #12100E;
            border-color: #F3E5AB;
        }
        .avatar-circle.tier-silver {
            background: linear-gradient(135deg, #E0E5EC, #8A95A5);
            color: #12100E;
            border-color: #FFF;
        }
        .client-name-line {
            display: flex;
            align-items: center;
            gap: 6px;
            flex-wrap: wrap;
        }
        .client-name {
            font-size: 15px;
            font-weight: 700;
            color: var(--text-primary);
        }
        .serial-tag {
            font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
            font-size: 11px;
            color: var(--text-tertiary);
            background: rgba(255, 255, 255, 0.04);
            padding: 2px 6px;
            border-radius: 5px;
        }
        .vip-pill {
            font-size: 10.5px;
            font-weight: 700;
            padding: 2px 8px;
            border-radius: 99px;
            display: inline-block;
        }
        .client-meta {
            font-size: 12px;
            color: var(--text-secondary);
            margin-top: 4px;
            display: flex;
            flex-wrap: wrap;
            gap: 10px;
        }
        .client-msg-preview {
            font-size: 11.5px;
            color: var(--gold-light);
            margin-top: 4px;
            font-style: italic;
        }

        /* Visual Stamp Dots in Client Row */
        .stamp-progress-col {
            display: flex;
            flex-direction: column;
            gap: 5px;
        }
        .stamp-top-row {
            display: flex;
            align-items: baseline;
            justify-content: space-between;
            gap: 8px;
        }
        .stamp-big-num {
            font-size: 17px;
            font-weight: 800;
            color: var(--gold-light);
        }
        .stamp-visits-meta {
            font-size: 11.5px;
            color: var(--text-secondary);
        }
        .stamp-dots-row {
            display: flex;
            gap: 5px;
            align-items: center;
        }
        .s-dot {
            width: 12px;
            height: 12px;
            border-radius: 50%;
            border: 1.5px solid rgba(198, 166, 117, 0.35);
            background: transparent;
            display: inline-block;
        }
        .s-dot.filled {
            background: var(--gold);
            border-color: #F3E5AB;
            box-shadow: 0 0 6px rgba(212, 175, 55, 0.45);
        }
        .s-dot.full-10 {
            background: var(--apple-green);
            border-color: #A8F5BC;
            box-shadow: 0 0 8px rgba(48, 209, 88, 0.6);
        }

        /* Telemetry Column */
        .telemetry-col {
            display: flex;
            flex-direction: column;
            gap: 4px;
            align-items: flex-start;
        }
        .status-pill-on {
            background: rgba(48, 209, 88, 0.14);
            border: 1px solid rgba(48, 209, 88, 0.35);
            color: #4AE072;
            font-size: 11px;
            font-weight: 700;
            padding: 3px 9px;
            border-radius: 99px;
        }
        .status-pill-off {
            background: rgba(255, 255, 255, 0.05);
            border: 1px solid rgba(255, 255, 255, 0.1);
            color: var(--text-secondary);
            font-size: 11px;
            padding: 3px 9px;
            border-radius: 99px;
        }
        .tele-line {
            font-size: 11px;
            color: var(--text-secondary);
        }

        /* Tactile Actions Column */
        .row-actions {
            display: flex;
            align-items: center;
            gap: 6px;
            flex-wrap: wrap;
            justify-content: flex-end;
        }
        .btn-stamp-plus {
            background: linear-gradient(135deg, #D4AF37 0%, #B38A22 100%);
            color: #12100E;
            border: none;
            border-radius: 11px;
            padding: 9px 14px;
            font-size: 13px;
            font-weight: 800;
            cursor: pointer;
            box-shadow: 0 4px 12px rgba(212, 175, 55, 0.22);
            transition: transform 0.12s ease;
        }
        .btn-stamp-plus:active { transform: scale(0.96); }
        .btn-redeem-glow {
            background: linear-gradient(135deg, #30D158 0%, #229E42 100%);
            color: #09140C;
            border: 1px solid #98F7AE;
            border-radius: 11px;
            padding: 9px 14px;
            font-size: 13px;
            font-weight: 800;
            cursor: pointer;
            animation: redeemPulse 1.6s infinite;
        }
        @keyframes redeemPulse {
            0%, 100% { box-shadow: 0 0 0 0 rgba(48, 209, 88, 0.6); }
            50% { box-shadow: 0 0 0 8px rgba(48, 209, 88, 0); }
        }
        .btn-redeem-normal {
            background: rgba(211, 84, 0, 0.18);
            border: 1px solid rgba(211, 84, 0, 0.4);
            color: #FF9F43;
            border-radius: 10px;
            padding: 8px 11px;
            font-size: 12px;
            font-weight: 600;
            cursor: pointer;
        }
        .btn-icon-sec {
            background: rgba(255, 255, 255, 0.05);
            border: 1px solid var(--border-subtle);
            color: var(--text-secondary);
            border-radius: 10px;
            padding: 8px 10px;
            font-size: 12px;
            font-weight: 600;
            cursor: pointer;
        }
        .btn-icon-sec:hover {
            background: rgba(255, 255, 255, 0.1);
            color: var(--text-primary);
        }
        .btn-icon-del:hover {
            background: rgba(255, 55, 95, 0.2);
            border-color: rgba(255, 55, 95, 0.45);
            color: #FF6B88;
        }
    </style>
</head>
<body>

    <!-- Dynamic Island Toast Notification -->
    {% if ok %}
    <div class="dynamic-island-wrap" id="status-toast">
        <div class="dynamic-island">
            <span class="di-dot"></span>
            <span style="flex:1;">{{ ok }}</span>
            <button class="di-close" onclick="document.getElementById('status-toast').style.display='none'">✕</button>
        </div>
    </div>
    {% endif %}

    <!-- Cabecera Flotante (Sticky Glass Header) -->
    <header class="sticky-header">
        <div class="header-inner">
            <div class="brand-Cluster">
                <div class="brand-emblem">☕️</div>
                <div>
                    <h1 class="brand-title">Maishas Café</h1>
                    <span class="brand-sub">Apple Wallet POS Console</span>
                </div>
            </div>

            <div class="kpi-cluster">
                <div class="kpi-pill">👥 Clientes <strong>{{ clients|length }}</strong></div>
                <div class="kpi-pill">📲 En iPhone <strong>{{ installed_count }}</strong></div>
                <div class="kpi-pill">🎁 Canjeados <strong>{{ total_redeemed }}</strong></div>
                <div class="kpi-pill">♛ VIP <strong>{{ gold_count + silver_count }}</strong></div>
                <div class="kpi-pill kpi-bday" onclick="setFilterTab('bday')" title="Ver cumpleañeros">
                    🎂 Cumpleaños Semana <strong>{{ upcoming_birthdays|length }}</strong>
                </div>
            </div>

            <div class="spotlight-wrap">
                <span class="spotlight-icon">🔍</span>
                <input type="text" id="client-search" class="spotlight-input" placeholder="Buscar nombre, código, celular..." oninput="filterClients()">
                <span class="spotlight-kbd">⌘K</span>
            </div>
        </div>
    </header>

    <main class="main-container">
        <!-- BENTO GRID SUPERIOR (3 MÓDULOS) -->
        <section class="bento-grid">

            <!-- MÓDULO 1: ESCÁNER QR CON HUD ESTILO CÁMARA iOS -->
            <div class="bento-card">
                <div>
                    <div class="bento-header">
                        <h2 class="bento-title">📷 Escáner QR Mostrador</h2>
                        <div class="segmented-control" title="Modo de acción al escanear QR">
                            <button type="button" id="seg-auto-on" class="seg-btn active" onclick="setAutoScanMode(true)">⚡ +1 Auto</button>
                            <button type="button" id="seg-auto-off" class="seg-btn" onclick="setAutoScanMode(false)">🔍 Manual</button>
                        </div>
                    </div>
                    <p class="bento-subtitle">Apunta al pase en el iPhone del cliente o usa un lector físico USB/Bluetooth.</p>

                    <div class="qr-hud-wrapper" id="qr-hud-box">
                        <div class="scan-flash" id="scan-flash-overlay"></div>
                        <video id="qr-video" playsinline muted></video>
                        <div class="qr-reticle" id="qr-reticle-guide"></div>
                        <canvas id="qr-canvas" style="display:none;"></canvas>

                        <div class="qr-idle-placeholder" id="qr-idle-view">
                            <button type="button" id="btn-toggle-cam" class="btn-cam-launch" onclick="toggleScanner()">
                                <span>📷 Activar Cámara QR</span>
                            </button>
                            <div style="font-size:11px; color:var(--text-tertiary); margin-top:8px;">Compatible con Safari iOS, iPadOS y Mac</div>
                        </div>
                    </div>
                    <div id="qr-status" style="font-size:12px; color:var(--gold-light); margin-bottom:8px; display:none; text-align:center;"></div>
                </div>

                <form id="scan-form" action="/api/scan-action" method="POST" class="quick-scan-form">
                    <input type="hidden" id="auto-stamp-flag" value="1">
                    <input type="text" id="scan-serial-input" name="serial" class="input-dark" placeholder="Serial ej. MAISHAS-AD58E5" required>
                    <input type="hidden" name="action" id="scan-action-input" value="add">
                    <button type="submit" class="btn-gold-sm">+1 Sello ☕️</button>
                </form>
            </div>

            <!-- MÓDULO 2: 🎂 RADAR DE CUMPLEAÑOS (APPLE CALENDAR WIDGET) -->
            <div class="bento-card bday-card-widget">
                <div>
                    <div class="bento-header">
                        <h2 class="bento-title">🎂 Radar de Cumpleaños</h2>
                        <span style="font-size:11px; font-weight:700; color:#FF6B88; background:rgba(255,55,95,0.14); padding:4px 9px; border-radius:99px;">
                            Hoy y próx. 7 días
                        </span>
                    </div>
                    <p class="bday-subtitle bento-subtitle">Envía una felicitación o regalo directo a su Apple Wallet con 1 clic.</p>

                    <div class="bday-list">
                        {% for bclient in upcoming_birthdays %}
                        <div class="bday-item {{ 'is-today' if bclient.bday.is_today else '' }}">
                            <div style="min-width:0;">
                                <div style="display:flex; align-items:center; gap:6px; flex-wrap:wrap;">
                                    <strong style="font-size:13.5px; color:var(--text-primary);">{{ bclient.name }}</strong>
                                    <span class="{{ 'bday-badge-today' if bclient.bday.is_today else 'bday-badge-soon' }}">
                                        {{ bclient.bday.badge_text }}
                                    </span>
                                </div>
                                <div style="font-size:11px; color:var(--text-secondary); margin-top:2px;">
                                    {{ bclient.tier.badge }} · {{ bclient.stamps }}/10 sellos
                                </div>
                            </div>
                            <form action="/api/send-message" method="POST" style="margin:0;">
                                <input type="hidden" name="target" value="{{ bclient.serial }}">
                                <input type="hidden" name="message" value="🎂 ¡Feliz cumpleaños, {{ bclient.first_name }}! Te esperamos en Maishas Café con una sorpresa especial para celebrar tu día ☕️✨">
                                <button type="submit" class="btn-bday-gift" title="Enviar regalo Push a {{ bclient.name }}">
                                    🎁 Enviar Regalo Push
                                </button>
                            </form>
                        </div>
                        {% else %}
                        <div style="text-align:center; padding:26px 12px; color:var(--text-secondary); font-size:12.5px; background:rgba(0,0,0,0.2); border-radius:14px;">
                            <div style="font-size:22px; margin-bottom:6px;">🗓️</div>
                            No hay cumpleaños registrados para hoy ni en los próximos 7 días.
                            {% if month_birthdays_count > 0 %}
                            <div style="margin-top:6px; color:var(--gold-light); cursor:pointer; font-weight:600;" onclick="setFilterTab('bday')">
                                Ver {{ month_birthdays_count }} cumpleañero(s) de este mes →
                            </div>
                            {% endif %}
                        </div>
                        {% endfor %}
                    </div>
                </div>

                <div style="font-size:11px; color:var(--text-secondary); margin-top:10px; display:flex; justify-content:space-between;">
                    <span>🎂 Cumpleañeros del mes: <strong>{{ month_birthdays_count }}</strong></span>
                    <a href="javascript:void(0)" onclick="setFilterTab('bday')" style="color:#FF6B88; text-decoration:none; font-weight:600;">Filtrar en lista ↓</a>
                </div>
            </div>

            <!-- MÓDULO 3: CENTRO DE MARKETING PUSH & GEOLOCALIZACIÓN GPS -->
            <div class="bento-card">
                <div>
                    <div class="bento-header">
                        <h2 class="bento-title" id="mkt-card-title">📣 Marketing Push</h2>
                        <div class="segmented-control">
                            <button type="button" id="tab-btn-push" class="seg-btn active" onclick="switchMktTab('push')">📣 Push</button>
                            <button type="button" id="tab-btn-gps" class="seg-btn" onclick="switchMktTab('gps')">📍 GPS ({{ "%.2f"|format(shop_loc.lat) }})</button>
                        </div>
                    </div>

                    <!-- Pestaña A: Marketing Push con Vista Previa Lock Screen -->
                    <div id="mkt-pane-push">
                        <div class="chip-row">
                            <button type="button" class="action-chip" onclick="setTemplate('🥐 ¡Recién salidos del horno! Ven por tu pan calientito y suma tu sello de hoy ☕️')">🥐 Pan horneado</button>
                            <button type="button" class="action-chip" onclick="setTemplate('✨ ¡Hora Feliz en Maishas Café! 2x1 en bebidas seleccionadas hoy de 4 a 6 pm ☕️')">✨ Hora Feliz 2x1</button>
                            <button type="button" class="action-chip" onclick="setTemplate('🔥 ¡Hoy tus compras valen DOBLE sello en Maishas Café! Te esperamos ☕️')">🔥 Doble Sello</button>
                        </div>

                        <form action="/api/send-message" method="POST">
                            <textarea name="message" id="push-msg-box" class="input-dark" maxlength="200" rows="2" placeholder="Escribe una promoción o aviso para la pantalla bloqueada..." required oninput="updateLockScreenPreview(this.value)"></textarea>

                            <!-- Vista Previa iPhone Lock Screen -->
                            <div class="ios-lockscreen-preview">
                                <div class="ios-notif-icon">☕️</div>
                                <div class="ios-notif-body">
                                    <div class="ios-notif-top">
                                        <span>MAISHAS CAFÉ</span>
                                        <span>ahora</span>
                                    </div>
                                    <div class="ios-notif-text" id="ios-preview-text">📣 Escribe un mensaje para previsualizar...</div>
                                </div>
                            </div>

                            <div style="display:flex; gap:8px; margin-top:8px;">
                                <select name="target" id="push-target-select" class="input-dark" style="flex:1;">
                                    <option value="all">📲 Todos ({{ installed_count }} iPhones activos)</option>
                                    {% for client in clients %}
                                    <option value="{{ client.serial }}">{{ client.name }} ({{ client.serial }})</option>
                                    {% endfor %}
                                </select>
                                <button type="submit" class="btn-cam-launch" style="white-space:nowrap;">Enviar Push 📲</button>
                            </div>
                        </form>
                    </div>

                    <!-- Pestaña B: Geolocalización GPS -->
                    <div id="mkt-pane-gps" style="display:none;">
                        <p class="bento-subtitle">Alerta automática en pantalla bloqueada cuando el cliente pasa a ~150m de la cafetería.</p>
                        <form action="/api/update-location" method="POST">
                            <div style="display:grid; grid-template-columns: 1fr 1fr auto; gap:8px; margin-bottom:8px;">
                                <div>
                                    <label style="font-size:10.5px; color:var(--gold-light);">Latitud</label>
                                    <input type="text" id="lat-input" name="lat" class="input-dark" value="{{ shop_loc.lat }}" required>
                                </div>
                                <div>
                                    <label style="font-size:10.5px; color:var(--gold-light);">Longitud</label>
                                    <input type="text" id="lng-input" name="lng" class="input-dark" value="{{ shop_loc.lng }}" required>
                                </div>
                                <div style="display:flex; align-items:flex-end;">
                                    <button type="button" class="btn-gold-sm" onclick="detectMyGPS()" title="Usar GPS actual">📍 Mi GPS</button>
                                </div>
                            </div>
                            <div style="margin-bottom:10px;">
                                <label style="font-size:10.5px; color:var(--gold-light);">Mensaje al pasar cerca:</label>
                                <input type="text" name="relevant_text" class="input-dark" value="{{ shop_loc.relevant_text }}" maxlength="160" required>
                            </div>
                            <button type="submit" class="btn-cam-launch" style="width:100%; justify-content:center;">
                                💾 Guardar GPS y Sincronizar Pases
                            </button>
                        </form>
                    </div>
                </div>
            </div>
        </section>

        <!-- LISTA DE CLIENTES DE LECTURA ULTRA-RÁPIDA -->
        <section class="clients-section">
            <div class="clients-top-bar">
                <div class="filter-tabs" id="filter-tabs-bar">
                    <button type="button" class="filter-pill active" data-tab="all" onclick="setFilterTab('all')">Todos ({{ clients|length }})</button>
                    <button type="button" class="filter-pill" data-tab="installed" onclick="setFilterTab('installed')">📲 En iPhone ({{ installed_count }})</button>
                    <button type="button" class="filter-pill" data-tab="ready" onclick="setFilterTab('ready')">🎁 Listos para Canjear ({{ ready_to_redeem_count }})</button>
                    <button type="button" class="filter-pill" data-tab="bday" onclick="setFilterTab('bday')">🎂 Cumpleaños ({{ upcoming_birthdays|length + month_birthdays_count }})</button>
                    <button type="button" class="filter-pill" data-tab="vip" onclick="setFilterTab('vip')">♛ Socios VIP ({{ gold_count + silver_count }})</button>
                </div>
            </div>

            <div class="client-list" id="clients-list-container">
                {% for client in clients %}
                <div id="row-{{ client.serial }}"
                     class="client-card-row client-row {{ 'highlight-row' if updated_serial == client.serial else '' }}"
                     data-search="{{ (client.name ~ ' ' ~ client.serial ~ ' ' ~ client.email ~ ' ' ~ client.phone ~ ' ' ~ client.tier.badge)|lower }}"
                     data-installed="{{ '1' if client.is_installed else '0' }}"
                     data-ready="{{ '1' if client.stamps >= 10 else '0' }}"
                     data-bday="{{ '1' if (client.bday.is_upcoming_week or client.bday.is_this_month) else '0' }}"
                     data-vip="{{ '1' if client.total_visits >= 10 else '0' }}">

                    <!-- Columna 1: Identidad, Avatar, Nivel VIP y Metadatos -->
                    <div class="client-identity">
                        <div class="avatar-circle tier-{{ client.tier.key }}">{{ client.initials }}</div>
                        <div style="min-width:0;">
                            <div class="client-name-line">
                                <span class="client-name">{{ client.name }}</span>
                                <span class="serial-tag">{{ client.serial }}</span>
                                <span class="vip-pill" style="background:{{ client.tier.bg }}; color:{{ client.tier.fg }}; border:1px solid {{ client.tier.border }};">
                                    {{ client.tier.badge }}
                                </span>
                                {% if client.bday.has_birthdate %}
                                <span class="{{ 'bday-badge-today' if client.bday.is_today else 'bday-badge-soon' }}">
                                    {{ client.bday.badge_text }}
                                </span>
                                {% endif %}
                            </div>
                            <div class="client-meta">
                                {% if client.email %}<span>✉️ {{ client.email }}</span>{% endif %}
                                {% if client.phone %}<span>📱 {{ client.phone }}</span>{% endif %}
                            </div>
                            {% if client.message %}
                            <div class="client-msg-preview">💬 "{{ client.message }}"</div>
                            {% endif %}
                        </div>
                    </div>

                    <!-- Columna 2: Barra de Progreso Visual de 10 Sellos -->
                    <div class="stamp-progress-col">
                        <div class="stamp-top-row">
                            <span class="stamp-big-num">{{ client.stamps }} / 10</span>
                            <span class="stamp-visits-meta">
                                ☕️ {{ client.total_visits }} visitas
                                {% if client.redeemed_count > 0 %} · 🎁 {{ client.redeemed_count }} canjes{% endif %}
                            </span>
                        </div>
                        <div class="stamp-dots-row" aria-label="{{ client.stamps }} de 10 sellos">
                            {% for idx in range(10) %}
                            <span class="s-dot {{ 'full-10' if client.stamps >= 10 else ('filled' if idx < client.stamps else '') }}"></span>
                            {% endfor %}
                        </div>
                    </div>

                    <!-- Columna 3: Telemetría Apple Wallet -->
                    <div class="telemetry-col">
                        {% if client.is_installed %}
                        <span class="status-pill-on">📲 Activo en iPhone ({{ client.device_count }})</span>
                        {% else %}
                        <span class="status-pill-off">⏳ Pendiente de instalar</span>
                        {% endif %}
                        {% if client.last_push_status %}
                        <span class="tele-line">📡 {{ client.last_push_status }}</span>
                        {% endif %}
                        {% if client.last_sync_at %}
                        <span class="tele-line" style="color:#7CE8B5;">🔄 Sincronizado: {{ client.last_sync_at }}</span>
                        {% endif %}
                    </div>

                    <!-- Columna 4: Botones Táctiles de Acción -->
                    <div class="row-actions">
                        <form action="/api/add-stamp" method="POST" style="margin:0;">
                            <input type="hidden" name="serial" value="{{ client.serial }}">
                            <input type="hidden" name="action" value="add">
                            <button type="submit" class="btn-stamp-plus" title="Sumar 1 sello a {{ client.name }}">+1 Sello ☕️</button>
                        </form>

                        {% if client.stamps >= 10 %}
                        <form action="/api/add-stamp" method="POST" style="margin:0;" onsubmit="return confirm('¿Canjear 1 Café Gratis y reiniciar a 0/10 sellos para {{ client.name }}?');">
                            <input type="hidden" name="serial" value="{{ client.serial }}">
                            <input type="hidden" name="action" value="reset">
                            <button type="submit" class="btn-redeem-glow">🎁 Canjear Café</button>
                        </form>
                        {% elif client.stamps > 0 %}
                        <form action="/api/add-stamp" method="POST" style="margin:0;" onsubmit="return confirm('¿Canjear recompensa anticipada y reiniciar a 0/10 para {{ client.name }}?');">
                            <input type="hidden" name="serial" value="{{ client.serial }}">
                            <input type="hidden" name="action" value="reset">
                            <button type="submit" class="btn-redeem-normal" title="Canjear y reiniciar a 0">🎁 Canjear</button>
                        </form>
                        {% endif %}

                        {% if client.stamps > 0 %}
                        <form action="/api/add-stamp" method="POST" style="margin:0;">
                            <input type="hidden" name="serial" value="{{ client.serial }}">
                            <input type="hidden" name="action" value="sub">
                            <button type="submit" class="btn-icon-sec" title="Restar 1 sello">-1</button>
                        </form>
                        {% endif %}

                        <button type="button" class="btn-icon-sec" title="Enviar mensaje directo a {{ client.name }}" onclick="selectClientForPush('{{ client.serial }}', '{{ client.name }}')">💬</button>

                        <form action="/api/delete-client" method="POST" style="margin:0;" onsubmit="return confirm('¿Eliminar permanentemente a {{ client.name }} ({{ client.serial }})?');">
                            <input type="hidden" name="serial" value="{{ client.serial }}">
                            <button type="submit" class="btn-icon-sec btn-icon-del" title="Eliminar registro">🗑️</button>
                        </form>
                    </div>
                </div>
                {% else %}
                <div style="text-align:center; padding:40px 20px; color:var(--text-secondary);">
                    No hay clientes registrados todavía. Comparte el enlace principal para empezar.
                </div>
                {% endfor %}
            </div>
        </section>
    </main>

    <script>
        // 1. Limpiar parámetros ?ok=... de la URL sin recargar
        if (window.history.replaceState && window.location.search) {
            window.history.replaceState({}, document.title, window.location.pathname);
        }

        // Auto-ocultar Dynamic Island Toast después de 6 segundos
        const toastEl = document.getElementById('status-toast');
        if (toastEl) {
            setTimeout(() => {
                toastEl.style.transition = 'opacity 0.4s ease';
                toastEl.style.opacity = '0';
                setTimeout(() => toastEl.style.display = 'none', 400);
            }, 6500);
        }

        // 2. Atajo de teclado Spotlight (⌘K o Ctrl+K)
        document.addEventListener('keydown', (e) => {
            if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
                e.preventDefault();
                const searchInput = document.getElementById('client-search');
                if (searchInput) {
                    searchInput.focus();
                    searchInput.select();
                }
            }
        });

        // 3. Vista Previa de Notificación en Pantalla Bloqueada iOS
        function updateLockScreenPreview(val) {
            const preview = document.getElementById('ios-preview-text');
            const clean = (val || '').trim();
            preview.textContent = clean ? ('📣 ' + clean) : '📣 Escribe un mensaje para previsualizar...';
        }

        function setTemplate(text) {
            const box = document.getElementById('push-msg-box');
            if (box) {
                box.value = text;
                updateLockScreenPreview(text);
                box.focus();
            }
        }

        function selectClientForPush(serial, name) {
            switchMktTab('push');
            const sel = document.getElementById('push-target-select');
            const box = document.getElementById('push-msg-box');
            if (sel) sel.value = serial;
            if (box) {
                box.focus();
                window.scrollTo({ top: 0, behavior: 'smooth' });
            }
        }

        function switchMktTab(tab) {
            const pushPane = document.getElementById('mkt-pane-push');
            const gpsPane = document.getElementById('mkt-pane-gps');
            const btnPush = document.getElementById('tab-btn-push');
            const btnGps = document.getElementById('tab-btn-gps');
            const title = document.getElementById('mkt-card-title');
            if (tab === 'gps') {
                pushPane.style.display = 'none';
                gpsPane.style.display = 'block';
                btnPush.classList.remove('active');
                btnGps.classList.add('active');
                title.textContent = '📍 Geolocalización GPS';
            } else {
                pushPane.style.display = 'block';
                gpsPane.style.display = 'none';
                btnPush.classList.add('active');
                btnGps.classList.remove('active');
                title.textContent = '📣 Marketing Push';
            }
        }

        // 4. Filtrado combinado por Pestaña + Spotlight Search
        let activeTab = 'all';

        function setFilterTab(tab) {
            activeTab = tab;
            document.querySelectorAll('.filter-pill').forEach(btn => {
                btn.classList.toggle('active', btn.getAttribute('data-tab') === tab);
            });
            filterClients();
        }

        function filterClients() {
            const q = (document.getElementById('client-search').value || '').toLowerCase().trim();
            const rows = document.querySelectorAll('.client-row');
            rows.forEach(row => {
                const searchTxt = row.getAttribute('data-search') || '';
                const matchesQuery = !q || searchTxt.includes(q);
                let matchesTab = true;
                if (activeTab === 'installed') matchesTab = row.getAttribute('data-installed') === '1';
                else if (activeTab === 'ready') matchesTab = row.getAttribute('data-ready') === '1';
                else if (activeTab === 'bday') matchesTab = row.getAttribute('data-bday') === '1';
                else if (activeTab === 'vip') matchesTab = row.getAttribute('data-vip') === '1';

                row.style.display = (matchesQuery && matchesTab) ? '' : 'none';
            });
        }

        // 5. Detectar GPS del navegador
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

        // 6. Escáner QR con HUD estilo Cámara iOS
        let videoStream = null;
        let scanning = false;
        let autoScanMode = true;

        function setAutoScanMode(enabled) {
            autoScanMode = enabled;
            document.getElementById('auto-stamp-flag').value = enabled ? '1' : '0';
            document.getElementById('seg-auto-on').classList.toggle('active', enabled);
            document.getElementById('seg-auto-off').classList.toggle('active', !enabled);
        }

        function playBeep() {
            try {
                const ctx = new (window.AudioContext || window.webkitAudioContext)();
                const osc = ctx.createOscillator();
                const gain = ctx.createGain();
                osc.type = 'sine';
                osc.frequency.setValueAtTime(920, ctx.currentTime);
                gain.gain.setValueAtTime(0.16, ctx.currentTime);
                osc.connect(gain);
                gain.connect(ctx.destination);
                osc.start();
                osc.stop(ctx.currentTime + 0.12);
            } catch (e) {}
        }

        async function toggleScanner() {
            const video = document.getElementById('qr-video');
            const idleView = document.getElementById('qr-idle-view');
            const reticle = document.getElementById('qr-reticle-guide');
            const status = document.getElementById('qr-status');

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
                idleView.style.display = 'none';
                video.style.display = 'block';
                reticle.style.display = 'block';
                status.style.display = 'block';
                status.textContent = 'Apunta al código QR del Apple Wallet (Clic en el video para cerrar)';
                video.onclick = stopScanner;
                requestAnimationFrame(scanTick);
            } catch (err) {
                alert('No se pudo abrir la cámara (verifica permisos en Safari/Chrome): ' + err.message);
            }
        }

        function stopScanner() {
            scanning = false;
            const video = document.getElementById('qr-video');
            const idleView = document.getElementById('qr-idle-view');
            const reticle = document.getElementById('qr-reticle-guide');
            const status = document.getElementById('qr-status');
            if (videoStream) {
                videoStream.getTracks().forEach(t => t.stop());
                videoStream = null;
            }
            video.style.display = 'none';
            reticle.style.display = 'none';
            idleView.style.display = 'block';
            status.style.display = 'none';
        }

        function scanTick() {
            if (!scanning) return;
            const video = document.getElementById('qr-video');
            const canvas = document.getElementById('qr-canvas');
            const status = document.getElementById('qr-status');
            const flash = document.getElementById('scan-flash-overlay');

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
                            if (flash) {
                                flash.style.opacity = '1';
                                setTimeout(() => { flash.style.opacity = '0'; }, 260);
                            }
                            document.getElementById('scan-serial-input').value = detected;
                            stopScanner();
                            status.style.display = 'block';
                            status.textContent = '✅ Detectado: ' + detected;
                            if (autoScanMode) {
                                document.getElementById('scan-form').submit();
                            } else {
                                document.getElementById('client-search').value = detected;
                                filterClients();
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

        stamps = max(0, min(10, int(stamps_count or 0)))
        visits = total_visits if total_visits is not None else stamps
        tier = get_vip_tier(visits)

        # 🎨 1. Generar strip.png y strip@2x.png dinámicamente con Pillow reflejando los sellos reales
        render_dynamic_strip(stamps, temp_dir, total_visits=visits)

        # 🎨 2. Configurar pass.json con paleta Luxury Coffee y jerarquía limpia sin superposición
        pass_json_path = os.path.join(temp_dir, "pass.json")
        with open(pass_json_path, "r", encoding="utf-8") as f:
            pass_data = json.load(f)

        pass_data["serialNumber"] = serial_number
        pass_data["foregroundColor"] = "rgb(245, 242, 235)"
        pass_data["backgroundColor"] = "rgb(18, 16, 14)"
        pass_data["labelColor"] = "rgb(198, 166, 117)"
        pass_data["logoText"] = "MAISHAS CAFÉ"

        pass_data["barcodes"] = [
            {
                "format": "PKBarcodeFormatQR",
                "message": serial_number,
                "messageEncoding": "iso-8859-1",
                "altText": serial_number
            }
        ]

        # Mensaje dinámico en pantalla bloqueada según los sellos
        if stamps == 0:
            stamp_change_msg = "🎁 ¡Sellos actualizados en Maishas Café! Total: %@"
        elif stamps >= 10:
            stamp_change_msg = "🎉 ¡Felicidades! Completaste %@ sellos. ¡Tienes 1 Café Gratis en Maishas Café!"
        else:
            stamp_change_msg = "¡Has ganado 1 nuevo sello en Maishas Café! ☕️ Total: %@"

        pass_data["storeCard"]["headerFields"] = [
            {
                "key": "stamps",
                "label": "SELLOS",
                "value": f"{stamps} / 10",
                "textAlignment": "PKTextAlignmentRight",
                "changeMessage": stamp_change_msg
            }
        ]

        # primaryFields vacío para que iOS NO dibuje texto encima de los 10 sellos del strip.png
        pass_data["storeCard"]["primaryFields"] = []

        reward_status = "¡CAFÉ GRATIS! 🎉" if stamps >= 10 else f"Faltan {10 - stamps} sellos"
        clean_visible_msg = (message or DEFAULT_MESSAGE).replace("\u200b", "")

        pass_data["storeCard"]["secondaryFields"] = [
            {
                "key": "memberName",
                "label": "CLIENTE",
                "value": client_name
            },
            {
                "key": "balance",
                "label": "NIVEL VIP",
                "value": tier["name"],
                "changeMessage": "🏆 ¡Subiste de nivel en Maishas Café! Ahora eres: %@"
            },
            {
                "key": "reward",
                "label": "PREMIO",
                "value": reward_status,
                "textAlignment": "PKTextAlignmentRight"
            }
        ]

        pass_data["storeCard"]["auxiliaryFields"] = [
            {
                "key": "promo",
                "label": "AVISO MAISHAS",
                "value": clean_visible_msg
            }
        ]

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

        # 📣 Reverso del pase: Mensaje con changeMessage para alertas Push + Historial VIP
        pass_data["storeCard"]["backFields"].insert(0, {
            "key": "news",
            "label": "Mensaje de Maishas Café",
            "value": message or DEFAULT_MESSAGE,
            "changeMessage": "📣 %@"
        })

        pass_data["storeCard"]["backFields"].insert(1, {
            "key": "vipHistory",
            "label": "Historial y Nivel de Socio",
            "value": f"Nivel: {tier['name']} | Visitas históricas: {visits} | Cafés gratis canjeados: {redeemed_count} ({tier['next_goal']})"
        })

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

        # 3. Calcular Manifest SHA1 (incluyendo los nuevos strip.png y strip@2x.png)
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

        # 4. Firmar con OpenSSL
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
    upcoming_birthdays = []
    installed_count = 0
    gold_count = 0
    silver_count = 0
    total_redeemed = 0
    ready_to_redeem_count = 0
    month_birthdays_count = 0

    for r in rows:
        reg_count = r[10] or 0
        is_installed = bool(r[4]) or reg_count > 0
        if is_installed:
            installed_count += 1
        stamps_val = r[3] or 0
        if stamps_val >= 10:
            ready_to_redeem_count += 1
        visits_val = r[11] if r[11] is not None else stamps_val
        redeemed_val = r[12] or 0
        total_redeemed += redeemed_val
        tier = get_vip_tier(visits_val)
        if visits_val >= 25:
            gold_count += 1
        elif visits_val >= 10:
            silver_count += 1

        full_name = (r[1] or "Cliente").strip()
        name_parts = [p for p in full_name.split() if p]
        if len(name_parts) >= 2:
            initials = (name_parts[0][0] + name_parts[1][0]).upper()
        elif name_parts:
            initials = name_parts[0][:2].upper()
        else:
            initials = "MC"
        first_name = name_parts[0] if name_parts else full_name

        bday_info = calculate_birthday_info(r[7] or "")
        if bday_info["is_this_month"]:
            month_birthdays_count += 1

        client_obj = {
            "serial": r[0],
            "name": full_name,
            "first_name": first_name,
            "initials": initials,
            "email": r[2] or "",
            "stamps": stamps_val,
            "push_token": r[4],
            "message": (r[5] or "").replace("\u200b", ""),
            "phone": r[6] or "",
            "birthdate": r[7] or "",
            "bday": bday_info,
            "last_push_status": r[8] or "",
            "last_sync_at": r[9] or "",
            "device_count": max(reg_count, 1 if r[4] else 0),
            "is_installed": is_installed,
            "total_visits": visits_val,
            "redeemed_count": redeemed_val,
            "tier": tier,
        }
        clients.append(client_obj)
        if bday_info["is_upcoming_week"]:
            upcoming_birthdays.append(client_obj)

    # Ordenar cumpleañeros de la semana: primero los que cumplen HOY (days_until == 0)
    upcoming_birthdays.sort(key=lambda x: x["bday"]["days_until"])

    return render_template_string(
        BARISTA_HTML,
        clients=clients,
        upcoming_birthdays=upcoming_birthdays,
        month_birthdays_count=month_birthdays_count,
        ready_to_redeem_count=ready_to_redeem_count,
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
        "SELECT serial, name, stamps, total_visits, redeemed_count, birthdate FROM clients WHERE UPPER(serial) = ?",
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
    bday_info = calculate_birthday_info(client[5] or "")
    bday_alert = ""
    if bday_info["is_today"]:
        bday_alert = " · 🎂 ¡HOY ES SU CUMPLEAÑOS!"
    elif bday_info["is_upcoming_week"]:
        bday_alert = f" · 🎂 Cumple {bday_info['badge_text']}"

    if action == "sub":
        new_stamps = max(0, current_stamps - 1)
        new_visits = max(0, current_visits - 1)
        new_redeemed = current_redeemed
        action_label = f"Sello restado a {client_name} ({serial_id}): {new_stamps}/10"
    elif action == "reset":
        new_stamps = 0
        new_visits = current_visits
        new_redeemed = current_redeemed + 1
        action_label = f"🎁 Café Gratis canjeado para {client_name} ({serial_id}): reiniciado a 0/10 (Total canjes: {new_redeemed}){bday_alert}"
    else:
        if current_stamps >= 10:
            conn.close()
            return serial_id, f"ℹ️ {client_name} ({serial_id}) ya tiene 10/10 sellos. Usa '🎁 Canjear' para entregar su café gratis.{bday_alert}"
        new_stamps = current_stamps + 1
        new_visits = current_visits + 1
        new_redeemed = current_redeemed
        old_tier = get_vip_tier(current_visits)["name"]
        new_tier = get_vip_tier(new_visits)["name"]
        tier_note = f" — 🏆 ¡Subió a {new_tier}!" if new_tier != old_tier else ""
        action_label = f"✅ Sello agregado a {client_name} ({serial_id}): ahora tiene {new_stamps}/10 ({new_visits} visitas){tier_note}{bday_alert}"

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
        ok=f"📍 Ubicación GPS guardada ({lat:.5f}, {lng:.5f}) y sincronizada con {pushed} iPhone(s) activos"
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


# 4. Descarga del pase actualizado (el iPhone baja la versión nueva con los sellos/mensajes/nivel/GPS/strip dinámico)
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
        try:
            ims_dt = parsedate_to_datetime(ims_header)
            if int(ims_dt.timestamp()) > upd_epoch_sec:
                conn.close()
                return "", 304
        except Exception:
            pass

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
        "dynamic_strip_pillow": False,
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
        test_path = generate_signed_pkpass("MAISHAS-DIAG01", "Prueba Diag", 4, request.host_url, "Diagnóstico OK", total_visits=12)
        report["pass_signing"] = os.path.isfile(test_path) and os.path.getsize(test_path) > 1000
        report["dynamic_strip_pillow"] = True
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
