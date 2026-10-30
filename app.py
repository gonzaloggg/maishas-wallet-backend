from flask import Flask, request, jsonify, send_file, render_template_string, redirect, url_for, Response
from werkzeug.middleware.proxy_fix import ProxyFix
from functools import wraps
from datetime import datetime, timezone, date, timedelta
from email.utils import formatdate, parsedate_to_datetime
from PIL import Image, ImageDraw, ImageFont
import os, io, csv, re, json, base64, hashlib, zipfile, subprocess, uuid, httpx, ssl, sqlite3, tempfile, shutil, math
import qrcode
from py_vapid import Vapid01, Vapid02
from pywebpush import webpush, WebPushException
from cryptography.hazmat.primitives import serialization

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
VAPID_FILE = os.path.join(DATA_DIR, "vapid_keys.json")


# Auto-inyectar certificados Apple desde Variables de Entorno en Railway
for env_var, file_path in [("ENV_KEY_PEM", KEY_PEM), ("ENV_CERT_PEM", CERT_PEM), ("ENV_WWDR_PEM", WWDR_PEM)]:
    val = os.environ.get(env_var)
    if val:
        val = val.replace('\\n', '\n').strip() + '\n'
        with open(file_path, 'w') as f:
            f.write(val)

AUTH_TOKEN = os.environ.get("AUTH_TOKEN", "MaishasSecretAuthToken2026")
PASS_TYPE_ID = "pass.com.maishascafe.loyalty"
DEFAULT_MESSAGE = "¡Bienvenido a Maishas Café! ☕️"
DEFAULT_LAT = 27.0706
DEFAULT_LNG = -109.4437
DEFAULT_GEO_TEXT = "☕️ ¡Estás cerca de Maishas Café! Pasa por tu café favorito y suma tu sello de hoy ✨"

# Zona horaria de Navojoa, Sonora (America/Hermosillo: UTC-7 fijo todo el año)
SONORA_TZ = timezone(timedelta(hours=-7))
MESES_ES = ["", "Ene", "Feb", "Mar", "Abr", "May", "Jun", "Jul", "Ago", "Sep", "Oct", "Nov", "Dic"]


def init_vapid_keys():
    """Inicializa o carga las llaves VAPID (ECDSA P-256) para Notificaciones Web Push en Android."""
    if os.path.isfile(VAPID_FILE):
        try:
            with open(VAPID_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if data.get("private_pem") and data.get("public_key"):
                    return data["private_pem"], data["public_key"]
        except Exception:
            pass

    v = Vapid01()
    v.generate_keys()
    priv_pem = v.private_pem().decode("utf-8")
    raw_pub = v.public_key.public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint
    )
    pub_b64 = base64.urlsafe_b64encode(raw_pub).rstrip(b"=").decode("utf-8")
    data = {"private_pem": priv_pem, "public_key": pub_b64}
    with open(VAPID_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    return priv_pem, pub_b64


VAPID_PRIVATE_PEM, VAPID_PUBLIC_KEY = init_vapid_keys()


def get_utc_timestamp():
    """Retorna timestamp UTC con precisión de microsegundos compatible con SQLite e iOS PassKit."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")


def get_short_time():
    """Retorna la hora local de Navojoa, Sonora (UTC-7) en formato sencillo de 12 horas para el barista."""
    return datetime.now(SONORA_TZ).strftime("%I:%M %p").lstrip("0")


def get_sonora_datetime_str():
    """Retorna fecha y hora local de Navojoa, Sonora (UTC-7) para registro de alta y reportes."""
    now_s = datetime.now(SONORA_TZ)
    return f"{now_s.day:02d}/{MESES_ES[now_s.month]}/{now_s.year} {now_s.strftime('%I:%M %p').lstrip('0')}"


def format_sonora_time(raw_val):
    """Convierte cualquier hora UTC antigua o timestamp a hora local amigable de Navojoa, Sonora."""
    if not raw_val:
        return ""
    s = str(raw_val).strip()
    # Si es un timestamp completo UTC (YYYY-MM-DD HH:MM:SS...)
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            dt_utc = datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
            dt_son = dt_utc.astimezone(SONORA_TZ)
            return f"{dt_son.day:02d}/{MESES_ES[dt_son.month]}/{dt_son.year} {dt_son.strftime('%I:%M %p').lstrip('0')}"
        except Exception:
            pass
    # Si contiene una hora corta en UTC del tipo '04:28:11 UTC'
    m = re.search(r"(\d{2}):(\d{2})(?::\d{2})?\s*UTC", s)
    if m:
        hh_utc, mm_utc = int(m.group(1)), int(m.group(2))
        hh_son = (hh_utc - 7) % 24
        ampm = "AM" if hh_son < 12 else "PM"
        hh_12 = hh_son % 12 or 12
        return f"{hh_12}:{mm_utc:02d} {ampm}"
    return s


def humanize_push_status(raw_status):
    """Traduce estados técnicos (como '200 OK' o 'UTC') a lenguaje claro para el barista en hora de Sonora."""
    if not raw_status:
        return ""
    s = str(raw_status).strip()
    t_son = format_sonora_time(s)
    # Extraer hora amigable si existe entre paréntesis
    m_par = re.search(r"\(([^)]+)\)", s)
    time_part = format_sonora_time(m_par.group(1)) if m_par else t_son
    if "200 OK" in s or "Notificación enviada" in s or "Live Sync" in s or "actualizada" in s:
        if time_part and time_part != s:
            return f"✅ Notificación enviada ({time_part})"
        return "✅ Notificación enviada"
    if "Error" in s:
        return f"⚠️ No se pudo notificar ({time_part})" if time_part else "⚠️ No se pudo notificar"
    return s


def get_vip_tier(total_visits):
    """Calcula el nivel VIP del cliente según su histórico acumulado de visitas."""
    visits = total_visits or 0
    if visits >= 25:
        return {
            "key": "gold",
            "name": "Socio Oro VIP ♛",
            "short": "Oro VIP ♛",
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
            "short": "Plata ✦",
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
            "short": "Bronce ☕️",
            "badge": "🌱 Bronce",
            "bg": "rgba(198, 166, 117, 0.16)",
            "fg": "#E6C280",
            "border": "rgba(198, 166, 117, 0.3)",
            "next_goal": f"A {10 - visits} visitas de Nivel Plata ✦"
        }


def calculate_birthday_info(birthdate_str, ref_date=None):
    """
    Analiza la fecha de nacimiento (YYYY-MM-DD) e identifica si el cliente cumple años
    HOY, en los próximos 7 días o en el mes actual (usando fecha local de Navojoa, Sonora).
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

    today = ref_date or datetime.now(SONORA_TZ).date()

    def _safe_date(yr, m, d):
        try:
            return date(yr, m, d)
        except ValueError:
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


def _clean_text_for_pil(text):
    """Limpia emojis y signos invertidos fuera de la fuente básica de Pillow para renderizado nítido."""
    if not text:
        return ""
    cleaned = "".join(ch for ch in text if 32 <= ord(ch) <= 255 and ch not in ("¡", "¿"))
    return " ".join(cleaned.split()).strip()


def _create_dynamic_strip_pil(stamps_count, message=None):
    """
    Crea el objeto PIL Image (1500x500 super-sampled) de la matriz Luxury Coffee de 10 sellos
    e incluye una barra inferior de alta legibilidad con el AVISO / estado del cliente.
    """
    stamps = max(0, min(10, int(stamps_count or 0)))
    W, H = 1500, 500
    img = Image.new("RGB", (W, H), (18, 16, 14))
    draw = ImageDraw.Draw(img)

    for y in range(H):
        t = y / float(H)
        mid = 1.0 - abs(t - 0.5) * 2.0
        if stamps >= 10:
            r = int(20 + 28 * mid)
            g = int(17 + 22 * mid)
            b = int(13 + 12 * mid)
        else:
            r = int(16 + 14 * mid)
            g = int(14 + 11 * mid)
            b = int(12 + 9 * mid)
        draw.line([(0, y), (W, y)], fill=(r, g, b))

    border_col = (212, 175, 55) if stamps >= 10 else (82, 68, 50)
    inner_border_col = (145, 118, 68) if stamps >= 10 else (50, 42, 33)
    draw.rounded_rectangle([24, 18, W - 25, H - 19], radius=36, outline=border_col, width=4)
    draw.rounded_rectangle([36, 30, W - 37, H - 31], radius=28, outline=inner_border_col, width=2)

    cols_x = [230, 490, 750, 1010, 1270]
    rows_y = [125, 282]
    radius = 58

    for idx in range(10):
        row = idx // 5
        col = idx % 5
        cx = cols_x[col]
        cy = rows_y[row]
        is_earned = (idx < stamps)
        is_tenth = (idx == 9)

        if is_earned:
            halo_r = radius + 10
            draw.ellipse(
                [cx - halo_r, cy - halo_r, cx + halo_r, cy + halo_r],
                fill=(48, 39, 24),
                outline=(110, 88, 44),
                width=2
            )
            fill_gold = (232, 196, 96) if stamps >= 10 else (212, 175, 55)
            rim_gold = (248, 232, 176)
            draw.ellipse(
                [cx - radius, cy - radius, cx + radius, cy + radius],
                fill=fill_gold,
                outline=rim_gold,
                width=5
            )
            in_r = radius - 12
            draw.ellipse(
                [cx - in_r, cy - in_r, cx + in_r, cy + in_r],
                outline=(150, 118, 30),
                width=3
            )

            if is_tenth:
                pts = []
                for i in range(10):
                    ang = -math.pi / 2 + i * (math.pi / 5)
                    rad_p = 26 if i % 2 == 0 else 11
                    pts.append((cx + rad_p * math.cos(ang), cy + rad_p * math.sin(ang)))
                draw.polygon(pts, fill=(22, 18, 15))
            else:
                bean_rx, bean_ry = 17, 23
                draw.ellipse(
                    [cx - bean_rx, cy - bean_ry, cx + bean_rx, cy + bean_ry],
                    fill=(24, 20, 16)
                )
                draw.arc(
                    [cx - 12, cy - bean_ry + 3, cx + 15, cy + bean_ry - 3],
                    start=110, end=250, fill=fill_gold, width=4
                )
        else:
            bg_pending = (22, 19, 16)
            ring_color = (158, 130, 84) if is_tenth else (76, 64, 50)
            ring_width = 6 if is_tenth else 4
            draw.ellipse(
                [cx - radius, cy - radius, cx + radius, cy + radius],
                fill=bg_pending,
                outline=ring_color,
                width=ring_width
            )
            in_r = radius - 13
            draw.ellipse(
                [cx - in_r, cy - in_r, cx + in_r, cy + in_r],
                outline=(52, 44, 35) if not is_tenth else (110, 90, 58),
                width=2
            )

            if is_tenth:
                pts = []
                for i in range(10):
                    ang = -math.pi / 2 + i * (math.pi / 5)
                    rad_p = 21 if i % 2 == 0 else 9
                    pts.append((cx + rad_p * math.cos(ang), cy + rad_p * math.sin(ang)))
                draw.polygon(pts, outline=(158, 130, 84), fill=(42, 35, 26))
            else:
                dot_r = 7
                draw.ellipse(
                    [cx - dot_r, cy - dot_r, cx + dot_r, cy + dot_r],
                    fill=(66, 56, 44)
                )

    # Barra inferior de alta legibilidad dentro del strip para que el AVISO siempre se lea completo
    raw_msg = _clean_text_for_pil((message or DEFAULT_MESSAGE).replace("\u200b", ""))
    if not raw_msg:
        raw_msg = "Bienvenido a Maishas Cafe"
    reward_txt = "CAFE GRATIS DISPONIBLE!" if stamps >= 10 else f"Faltan {10 - stamps} sellos para tu Cafe Gratis"
    banner_text = f"{reward_txt}  |  AVISO: {raw_msg}"
    if len(banner_text) > 88:
        banner_text = banner_text[:85] + "..."

    draw.rounded_rectangle(
        [64, 382, W - 64, 452],
        radius=22,
        fill=(26, 22, 18),
        outline=(128, 104, 62),
        width=2
    )
    try:
        font_banner = ImageFont.load_default(size=26)
    except TypeError:
        font_banner = ImageFont.load_default()

    bbox = draw.textbbox((0, 0), banner_text, font=font_banner)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    tx = max(80, (W - tw) // 2)
    ty = 382 + (70 - th) // 2 - 3
    draw.text((tx, ty), banner_text, fill=(238, 212, 152), font=font_banner)

    return img


def render_dynamic_strip(stamps_count, output_dir, total_visits=0, message=None):
    """
    Guarda strip.png (375x125) y strip@2x.png (750x250) para Apple Wallet .pkpass.
    """
    img = _create_dynamic_strip_pil(stamps_count, message=message)
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
            last_sync_at TEXT,
            platform TEXT DEFAULT 'ios',
            android_active INTEGER DEFAULT 0,
            created_at TEXT,
            last_action TEXT DEFAULT 'add'
        )
    ''')
    cols = [r[1] for r in c.execute("PRAGMA table_info(clients)")]
    migrations = [
        ("message", "ALTER TABLE clients ADD COLUMN message TEXT"),
        ("phone", "ALTER TABLE clients ADD COLUMN phone TEXT"),
        ("birthdate", "ALTER TABLE clients ADD COLUMN birthdate TEXT"),
        ("last_push_status", "ALTER TABLE clients ADD COLUMN last_push_status TEXT"),
        ("last_sync_at", "ALTER TABLE clients ADD COLUMN last_sync_at TEXT"),
        ("total_visits", "ALTER TABLE clients ADD COLUMN total_visits INTEGER DEFAULT 0"),
        ("redeemed_count", "ALTER TABLE clients ADD COLUMN redeemed_count INTEGER DEFAULT 0"),
        ("platform", "ALTER TABLE clients ADD COLUMN platform TEXT DEFAULT 'ios'"),
        ("android_active", "ALTER TABLE clients ADD COLUMN android_active INTEGER DEFAULT 0"),
        ("created_at", "ALTER TABLE clients ADD COLUMN created_at TEXT"),
        ("last_action", "ALTER TABLE clients ADD COLUMN last_action TEXT DEFAULT 'add'"),
    ]
    for col_name, sql in migrations:
        if col_name not in cols:
            c.execute(sql)

    c.execute("UPDATE clients SET total_visits = COALESCE(stamps, 0) WHERE total_visits IS NULL OR (total_visits = 0 AND stamps > 0)")
    c.execute("UPDATE clients SET redeemed_count = 0 WHERE redeemed_count IS NULL")
    c.execute("UPDATE clients SET android_active = 0 WHERE android_active IS NULL")
    c.execute("UPDATE clients SET last_action = 'add' WHERE last_action IS NULL OR last_action = ''")
    c.execute("UPDATE clients SET updated_at = ? WHERE updated_at IS NULL OR updated_at = ''", (get_utc_timestamp(),))
    c.execute("UPDATE clients SET created_at = ? WHERE created_at IS NULL OR created_at = ''", (get_sonora_datetime_str(),))
    c.execute(
        "UPDATE clients SET message = ? WHERE message = '¡Bienvenido a Maishas Café! Aquí verás nuestras promociones.'",
        (DEFAULT_MESSAGE,)
    )

    c.execute('''
        CREATE TABLE IF NOT EXISTS registrations (
            device_id TEXT,
            push_token TEXT,
            serial TEXT,
            push_service_url TEXT,
            PRIMARY KEY (device_id, serial)
        )
    ''')
    reg_cols = [r[1] for r in c.execute("PRAGMA table_info(registrations)")]
    if "push_service_url" not in reg_cols:
        c.execute("ALTER TABLE registrations ADD COLUMN push_service_url TEXT")

    c.execute('''
        CREATE TABLE IF NOT EXISTS android_subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            serial TEXT,
            endpoint TEXT UNIQUE,
            sub_json TEXT,
            created_at TEXT
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
# PLANTILLA 1: PORTAL PÚBLICO DE REGISTRO (iOS + ANDROID)
# ---------------------------------------------------------
REGISTRATION_HTML = """
<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>Maishas Café — Club de Fidelización Digital</title>
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
            --android-green: #3DDC84;
        }
        * { box-sizing: border-box; -webkit-font-smoothing: antialiased; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, "SF Pro Display", "SF Pro Text", "Roboto", sans-serif;
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
            gap: 18px;
        }
        .brand-header { text-align: center; }
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
            margin-bottom: 8px;
        }
        .brand-header h1 {
            margin: 0;
            font-size: 28px;
            font-weight: 700;
            letter-spacing: -0.5px;
        }
        .brand-header p {
            margin: 5px 0 0;
            font-size: 13.5px;
            color: var(--text-muted);
        }

        .form-card {
            background: var(--bg-surface);
            backdrop-filter: blur(24px);
            -webkit-backdrop-filter: blur(24px);
            border: 1px solid var(--border-glass);
            border-radius: 24px;
            padding: 26px 22px;
            box-shadow: 0 16px 40px rgba(0, 0, 0, 0.45);
        }
        .field-group { margin-bottom: 15px; text-align: left; }
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
        }
        input:focus {
            outline: none;
            border-color: var(--gold);
            box-shadow: 0 0 0 3px rgba(212, 175, 55, 0.2);
        }
        input[type="date"] { color-scheme: dark; }
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
        .btn-submit-wallet {
            width: 100%;
            margin-top: 10px;
            padding: 15px 18px;
            border-radius: 14px;
            border: 1px solid rgba(255, 255, 255, 0.18);
            background: linear-gradient(135deg, #D4AF37 0%, #B58E24 100%);
            color: #12100E;
            font-size: 16px;
            font-weight: 700;
            cursor: pointer;
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 8px;
            box-shadow: 0 8px 24px rgba(212, 175, 55, 0.3);
            transition: transform 0.15s ease, box-shadow 0.15s ease;
        }
        .btn-submit-wallet:active {
            transform: scale(0.98);
        }
        .safari-hint {
            text-align: center;
            font-size: 12px;
            color: var(--text-muted);
            margin-top: 12px;
            margin-bottom: 0;
        }
    </style>
</head>
<body>
    <div class="layout-wrapper">
        <div class="brand-header">
            <span class="brand-pill">☕️ Maishas Loyalty Club</span>
            <h1>Maishas Café</h1>
            <p>Regístrate y acumula sellos por cada café</p>
        </div>

        <!-- Formulario de Registro Limpio con Detección Automática de Dispositivo -->
        <div class="form-card">
            <form action="/api/register" method="POST">
                <input type="hidden" name="platform" id="platform-input" value="">

                <div class="field-group">
                    <label class="field-label"><span>Nombre y Apellido</span></label>
                    <input type="text" name="name" placeholder="Ej. Ana Sofía López" required>
                </div>

                <div class="field-group">
                    <label class="field-label"><span>Correo Electrónico</span></label>
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
                        <span>Recibe un regalo especial en tu tarjeta el día de tu cumpleaños</span>
                    </div>
                </div>

                <button type="submit" id="submit-wallet-btn" class="btn-submit-wallet">
                    <span>📲 Obtener mi Tarjeta Digital</span>
                </button>
            </form>
            <p class="safari-hint">Se instalará automáticamente en tu teléfono al registrarte.</p>
        </div>
    </div>

    <script>
        // Detección 100% automática y silenciosa del tipo de celular (iPhone vs Android)
        (function detectDeviceAutomatically() {
            const ua = navigator.userAgent || navigator.vendor || window.opera || '';
            const platInput = document.getElementById('platform-input');
            if (/android/i.test(ua)) {
                platInput.value = 'android';
            } else if (/iPad|iPhone|iPod/.test(ua) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1)) {
                platInput.value = 'ios';
            }
        })();
    </script>
</body>
</html>
"""


# ---------------------------------------------------------
# PLANTILLA 2: TARJETA DIGITAL INTERACTIVA ANDROID (PWA + WEB PUSH + LIVE SYNC + GPS)
# ---------------------------------------------------------
ANDROID_CARD_HTML = """
<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <meta name="theme-color" content="#12100E">
    <meta name="apple-mobile-web-app-capable" content="yes">
    <meta name="mobile-web-app-capable" content="yes">
    <link rel="manifest" href="/card/{{ client.serial }}/manifest.webmanifest">
    <link rel="icon" type="image/png" href="/card/icon-192.png">
    <title>Maishas Café — Tarjeta {{ client.name }}</title>
    <style>
        :root {
            --bg-deep: #0D0C0B;
            --card-bg: #12100E;
            --gold: #D4AF37;
            --gold-light: #E6C280;
            --label-gold: #C6A675;
            --text-cream: #F5F2EB;
            --text-muted: #A39B90;
            --apple-green: #30D158;
        }
        * { box-sizing: border-box; -webkit-font-smoothing: antialiased; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, "Roboto", "SF Pro Display", sans-serif;
            font-variant-numeric: tabular-nums;
            background-color: var(--bg-deep);
            background-image: radial-gradient(circle at 50% 0%, rgba(212, 175, 55, 0.14) 0%, transparent 60%);
            color: var(--text-cream);
            margin: 0;
            padding: 16px 14px 48px;
            min-height: 100vh;
            display: flex;
            flex-direction: column;
            align-items: center;
        }

        /* Banner de Notificación Push en Vivo (Estilo Lock Screen / Heads-up) */
        .push-banner {
            position: fixed;
            top: 14px;
            left: 50%;
            transform: translateX(-50%) translateY(-150%);
            z-index: 9999;
            width: 94%;
            max-width: 420px;
            background: rgba(24, 21, 18, 0.96);
            backdrop-filter: blur(24px);
            -webkit-backdrop-filter: blur(24px);
            border: 1px solid rgba(212, 175, 55, 0.55);
            box-shadow: 0 20px 50px rgba(0, 0, 0, 0.85), 0 0 25px rgba(212, 175, 55, 0.25);
            border-radius: 18px;
            padding: 13px 15px;
            display: flex;
            align-items: center;
            gap: 12px;
            transition: transform 0.45s cubic-bezier(0.16, 1, 0.3, 1);
        }
        .push-banner.show {
            transform: translateX(-50%) translateY(0);
        }
        .pb-icon {
            width: 38px;
            height: 38px;
            border-radius: 10px;
            background: linear-gradient(135deg, #D4AF37, #8F6B1A);
            color: #12100E;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 19px;
            flex-shrink: 0;
        }
        .pb-content { flex: 1; min-width: 0; }
        .pb-title {
            font-size: 11px;
            font-weight: 700;
            color: var(--gold-light);
            letter-spacing: 0.5px;
            text-transform: uppercase;
            display: flex;
            justify-content: space-between;
        }
        .pb-body {
            font-size: 13.5px;
            font-weight: 600;
            color: var(--text-cream);
            margin-top: 2px;
            line-height: 1.35;
        }

        /* Barra Superior de Estado Wallet */
        .wallet-top-bar {
            width: 100%;
            max-width: 410px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 12px;
            padding: 0 4px;
        }
        .sync-badge {
            display: inline-flex;
            align-items: center;
            gap: 6px;
            font-size: 11.5px;
            font-weight: 600;
            color: #4AE072;
            background: rgba(48, 209, 88, 0.12);
            border: 1px solid rgba(48, 209, 88, 0.3);
            padding: 5px 11px;
            border-radius: 99px;
        }
        .sync-dot {
            width: 7px;
            height: 7px;
            border-radius: 50%;
            background: #30D158;
            box-shadow: 0 0 8px #30D158;
            animation: blinkDot 2s infinite;
        }
        @keyframes blinkDot {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.35; }
        }
        .btn-flip {
            background: rgba(255, 255, 255, 0.08);
            border: 1px solid rgba(255, 255, 255, 0.14);
            color: var(--text-cream);
            border-radius: 99px;
            padding: 6px 13px;
            font-size: 12px;
            font-weight: 600;
            cursor: pointer;
        }

        /* Tarjeta Principal Estilo PassKit (Frente y Reverso) */
        .pass-card {
            width: 100%;
            max-width: 410px;
            background: linear-gradient(170deg, #181512 0%, #100E0C 100%);
            border-radius: 24px;
            border: 1px solid rgba(212, 175, 55, 0.38);
            box-shadow: 0 24px 60px rgba(0, 0, 0, 0.8), inset 0 1px 0 rgba(255, 255, 255, 0.12);
            overflow: hidden;
            position: relative;
        }
        .pass-header {
            padding: 16px 20px 14px;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        .pass-brand {
            display: flex;
            align-items: center;
            gap: 10px;
            font-weight: 800;
            font-size: 14px;
            letter-spacing: 0.8px;
        }
        .pass-brand img {
            width: 30px;
            height: 30px;
            border-radius: 50%;
        }
        .pass-header-field {
            text-align: right;
        }
        .ph-lbl {
            font-size: 10px;
            font-weight: 700;
            color: var(--label-gold);
            letter-spacing: 0.8px;
            display: block;
        }
        .ph-val {
            font-size: 19px;
            font-weight: 800;
            color: var(--text-cream);
        }

        /* Strip Dinámico Generado con Pillow */
        .pass-strip-wrap {
            width: 100%;
            background: #12100E;
            border-top: 1px solid rgba(198, 166, 117, 0.18);
            border-bottom: 1px solid rgba(198, 166, 117, 0.18);
            line-height: 0;
        }
        .pass-strip-wrap img {
            width: 100%;
            height: auto;
            display: block;
        }

        /* Campos Secundarios y Auxiliares */
        .pass-fields-grid {
            padding: 15px 20px 10px;
            display: grid;
            grid-template-columns: 1.25fr 1.1fr 1fr;
            gap: 10px;
        }
        .p-field .lbl {
            font-size: 9.5px;
            font-weight: 700;
            color: var(--label-gold);
            letter-spacing: 0.6px;
            text-transform: uppercase;
            display: block;
            margin-bottom: 3px;
        }
        .p-field .val {
            font-size: 13px;
            font-weight: 600;
            color: var(--text-cream);
            display: block;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }
        .pass-promo-row {
            padding: 8px 20px 14px;
            border-bottom: 1px dashed rgba(255, 255, 255, 0.08);
        }
        .pass-promo-row .lbl {
            font-size: 9.5px;
            font-weight: 700;
            color: var(--label-gold);
            letter-spacing: 0.6px;
            text-transform: uppercase;
            display: block;
            margin-bottom: 3px;
        }
        .pass-promo-row .val {
            font-size: 13px;
            color: var(--gold-light);
            font-weight: 500;
            line-height: 1.35;
        }

        /* Zona de Código QR de Alto Contraste para Escáner Mostrador */
        .pass-qr-zone {
            padding: 20px;
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
        }
        .qr-white-box {
            background: #FFFFFF;
            padding: 12px;
            border-radius: 16px;
            box-shadow: 0 8px 28px rgba(0, 0, 0, 0.6);
            display: inline-block;
        }
        .qr-white-box img {
            width: 168px;
            height: 168px;
            display: block;
        }
        .qr-alt-text {
            margin-top: 8px;
            font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
            font-size: 12.5px;
            color: var(--text-muted);
            letter-spacing: 1px;
        }

        /* Reverso de la Tarjeta (Detalles del Pase) */
        .pass-back-view {
            display: none;
            padding: 20px;
        }
        .back-section {
            padding: 12px 0;
            border-bottom: 1px solid rgba(255, 255, 255, 0.08);
        }
        .back-section:last-child { border-bottom: none; }
        .back-lbl {
            font-size: 10.5px;
            font-weight: 700;
            color: var(--label-gold);
            text-transform: uppercase;
            letter-spacing: 0.5px;
            margin-bottom: 4px;
        }
        .back-val {
            font-size: 13.5px;
            color: var(--text-cream);
            line-height: 1.45;
        }

        /* Caja Superior Destacada para Guardar en Billetera Android / Google Wallet */
        .wallet-cta-box {
            width: 100%;
            max-width: 410px;
            background: linear-gradient(160deg, rgba(34, 29, 22, 0.95) 0%, rgba(20, 17, 14, 0.95) 100%);
            border: 1.5px solid rgba(212, 175, 55, 0.55);
            border-radius: 20px;
            padding: 14px 16px;
            margin-bottom: 14px;
            box-shadow: 0 14px 34px rgba(0, 0, 0, 0.65), 0 0 22px rgba(212, 175, 55, 0.14);
            display: flex;
            flex-direction: column;
            gap: 10px;
        }
        .wallet-cta-title {
            font-size: 13px;
            font-weight: 700;
            color: var(--gold-light);
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 8px;
        }
        .wallet-step-hint {
            display: none;
            background: rgba(48, 209, 88, 0.14);
            border: 1px solid rgba(48, 209, 88, 0.4);
            color: #66F08B;
            border-radius: 12px;
            padding: 10px 12px;
            font-size: 12.5px;
            line-height: 1.4;
            font-weight: 600;
        }
        .wallet-sub-links {
            display: flex;
            gap: 8px;
            flex-wrap: wrap;
        }
        .btn-mini-intent {
            flex: 1;
            min-width: 140px;
            padding: 9px 11px;
            border-radius: 11px;
            border: 1px solid rgba(255, 255, 255, 0.14);
            background: rgba(255, 255, 255, 0.06);
            color: var(--text-cream);
            font-size: 12px;
            font-weight: 600;
            text-align: center;
            text-decoration: none;
            cursor: pointer;
            display: inline-flex;
            align-items: center;
            justify-content: center;
            gap: 6px;
        }

        /* Botones de Acción Android (Notificaciones Push e Instalar en Inicio) */
        .android-actions-stack {
            width: 100%;
            max-width: 410px;
            margin-top: 14px;
            display: flex;
            flex-direction: column;
            gap: 10px;
        }
        .btn-action-gold {
            width: 100%;
            padding: 15px 16px;
            border-radius: 14px;
            border: 1px solid rgba(255, 255, 255, 0.22);
            background: linear-gradient(135deg, #E2BE52 0%, #B88E24 100%);
            color: #12100E;
            font-size: 15px;
            font-weight: 800;
            cursor: pointer;
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 8px;
            text-decoration: none;
            box-shadow: 0 8px 24px rgba(212, 175, 55, 0.3);
        }
        .btn-action-glass {
            width: 100%;
            padding: 13px 16px;
            border-radius: 14px;
            border: 1px solid rgba(255, 255, 255, 0.12);
            background: rgba(255, 255, 255, 0.05);
            color: var(--text-cream);
            font-size: 13.5px;
            font-weight: 600;
            cursor: pointer;
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 8px;
            text-decoration: none;
        }
    </style>
</head>
<body>

    <!-- Banner de Notificación Push en Vivo -->
    <div class="push-banner" id="live-push-banner">
        <div class="pb-icon">☕️</div>
        <div class="pb-content">
            <div class="pb-title">
                <span>MAISHAS CAFÉ</span>
                <span>ahora</span>
            </div>
            <div class="pb-body" id="live-push-text">¡Tarjeta sincronizada!</div>
        </div>
    </div>

    <!-- Barra Superior -->
    <div class="wallet-top-bar">
        <div class="sync-badge">
            <span class="sync-dot"></span>
            <span id="sync-status-label">ID: {{ client.serial }} · Sincronizado</span>
        </div>
        <button type="button" class="btn-flip" id="btn-flip-toggle" onclick="toggleBackView()">
            ⓘ Detalles / Reverso
        </button>
    </div>

    <!-- CAJA PRINCIPAL ARRIBA: GUARDAR EN BILLETERA ANDROID + ACTIVAR PUSH EN VIVO -->
    <div class="wallet-cta-box">
        <div class="wallet-cta-title">
            <span>📲 Billetera Digital y Alertas en Vivo</span>
            <span style="font-size:11px; color:var(--gold); font-weight:700; font-family:ui-monospace, monospace;">{{ client.serial }}</span>
        </div>

        <div id="push-required-notice" style="background:rgba(212,175,55,0.13); border:1px solid rgba(212,175,55,0.4); border-radius:12px; padding:9px 12px; font-size:12px; color:#F5E3B3; line-height:1.4;">
            🔔 <strong>Importante en Android:</strong> Al tocar el botón dorado selecciona <strong>«Permitir notificaciones»</strong> para que tus sellos y avisos te lleguen al instante.
        </div>

        {% if gw_enabled %}
        <a href="/card/{{ client.serial }}/google-wallet"
           id="btn-google-wallet"
           class="btn-action-gold"
           style="background:#000; color:#fff; border:1.5px solid #5f6368; gap:10px;"
           onclick="onSaveToWalletClick()">
            <span style="font-size:20px;">🅖</span>
            <span id="btn-save-wallet-label">Agregar a Google Wallet · {{ client.stamps }}/10 sellos</span>
        </a>
        <div style="font-size:11.5px; color:var(--text-muted); text-align:center; line-height:1.4;">
            Tus sellos y avisos se actualizan solos dentro de Google Wallet, igual que en Apple Wallet.
        </div>
        {% else %}
        <a href="/card/{{ client.serial }}/download.pkpass?v={{ client.updated_at | urlencode }}"
           id="btn-save-wallet-top"
           class="btn-action-gold"
           onclick="onSaveToWalletClick()">
            <span id="btn-save-wallet-label">📲 Activar Alertas y Guardar en Wallet ({{ client.stamps }}/10)</span>
        </a>
        {% endif %}

        <div id="wallet-download-hint" class="wallet-step-hint">
            ✅ <strong>Pase {{ client.serial }} descargado ({{ client.stamps }}/10 sellos):</strong> Toca <strong>«Abrir»</strong> en la descarga para guardarlo en tu <strong>Billetera</strong>, y toca <strong>«Permitir»</strong> si tu celular te pregunta por las notificaciones.
        </div>

        <div class="wallet-sub-links">
            <button type="button" id="btn-enable-push-top" class="btn-mini-intent" onclick="enableAndroidPush(false)" style="border-color:rgba(212,175,55,0.45); color:#E6C280; width:100%;">
                🔔 Activar Notificaciones Push en este Android
            </button>
        </div>
    </div>

    <!-- TARJETA DIGITAL INTERACTIVA -->
    <div class="pass-card">
        <!-- VISTA FRONTAL -->
        <div id="pass-front-view">
            <div class="pass-header">
                <div class="pass-brand">
                    <img src="/card/icon-192.png" alt="Maishas">
                    <span>MAISHAS CAFÉ</span>
                </div>
                <div class="pass-header-field">
                    <span class="ph-lbl">SELLOS</span>
                    <span class="ph-val" id="val-stamps">{{ client.stamps }} / 10</span>
                </div>
            </div>

            <div class="pass-strip-wrap">
                <img id="card-strip-img" src="/card/{{ client.serial }}/strip.png?v={{ client.updated_at }}" alt="Sellos Maishas Café">
            </div>

            <div class="pass-fields-grid">
                <div class="p-field">
                    <span class="lbl">CLIENTE</span>
                    <span class="val" id="val-name">{{ client.name }}</span>
                </div>
                <div class="p-field">
                    <span class="lbl">NIVEL VIP</span>
                    <span class="val" id="val-tier">{{ client.tier.name }}</span>
                </div>
                <div class="p-field" style="text-align:right;">
                    <span class="lbl">PREMIO</span>
                    <span class="val" id="val-reward">{{ client.reward_status }}</span>
                </div>
            </div>

            <div class="pass-promo-row">
                <span class="lbl">AVISO MAISHAS</span>
                <span class="val" id="val-promo">{{ client.message }}</span>
            </div>

            <div class="pass-qr-zone">
                <div class="qr-white-box">
                    <img src="/card/{{ client.serial }}/qr.png" alt="QR {{ client.serial }}">
                </div>
                <div class="qr-alt-text">{{ client.serial }}</div>
            </div>
        </div>

        <!-- VISTA REVERSO (DETALLES DEL PASE) -->
        <div id="pass-back-view" class="pass-back-view">
            <div class="back-section">
                <div class="back-lbl">Código Único de Socio (ID)</div>
                <div class="back-val" style="font-family:ui-monospace, monospace; font-weight:700; color:var(--gold-light);">{{ client.serial }}</div>
            </div>
            <div class="back-section">
                <div class="back-lbl">Mensaje de Maishas Café</div>
                <div class="back-val" id="back-val-msg">{{ client.message }}</div>
            </div>
            <div class="back-section">
                <div class="back-lbl">Historial y Nivel de Socio</div>
                <div class="back-val" id="back-val-history">
                    Nivel: {{ client.tier.name }} | Visitas históricas: {{ client.total_visits }} | Cafés gratis canjeados: {{ client.redeemed_count }} ({{ client.tier.next_goal }})
                </div>
            </div>
            {% if client.phone or client.birthdate %}
            <div class="back-section">
                <div class="back-lbl">Datos del Socio</div>
                <div class="back-val">
                    {% if client.phone %}Celular: {{ client.phone }}{% endif %}
                    {% if client.phone and client.birthdate %} | {% endif %}
                    {% if client.birthdate %}Cumpleaños: {{ client.birthdate }}{% endif %}
                </div>
            </div>
            {% endif %}
            <div class="back-section">
                <div class="back-lbl">📍 Ubicación y Alertas de Proximidad</div>
                <div class="back-val">{{ shop_loc.relevant_text }}</div>
            </div>
            <div class="back-section">
                <div class="back-lbl">Términos y Condiciones</div>
                <div class="back-val">Acumula 1 sello por cada compra de café. Al completar 10 sellos, obtén un café de cualquier tamaño totalmente gratis. Válido en sucursal Maishas Café (@maishascafe).</div>
            </div>
        </div>
    </div>

    <!-- BOTONES SECUNDARIOS DE ACCIÓN -->
    <div class="android-actions-stack">
        <button type="button" id="btn-install-pwa" class="btn-action-glass" style="border-color:rgba(48,209,88,0.45); color:#66F08B;" onclick="installPWA()">
            <span>📲 Instalar Tarjeta Automática en Pantalla de Inicio</span>
        </button>

        <button type="button" id="btn-enable-push" class="btn-action-glass" style="border-color:rgba(212,175,55,0.35); color:#E6C280;" onclick="enableAndroidPush(false)">
            <span>🔔 Activar Notificaciones Push en mi Celular</span>
        </button>
    </div>

    <script>
        const SERIAL = "{{ client.serial }}";
        const VAPID_PUBLIC_KEY = "{{ vapid_public_key }}";
        const SHOP_LAT = {{ shop_loc.lat }};
        const SHOP_LNG = {{ shop_loc.lng }};
        const SHOP_GEO_TEXT = {{ shop_loc.relevant_text | tojson }};

        let currentUpdatedAt = {{ client.updated_at | tojson }};
        let currentStamps = {{ client.stamps }};
        let currentMessage = {{ client.message | tojson }};
        let currentTier = {{ client.tier.name | tojson }};
        let showingBack = false;

        function onSaveToWalletClick() {
            const hint = document.getElementById('wallet-download-hint');
            if (hint) hint.style.display = 'block';
            showLiveBanner('📲 Pase ' + SERIAL + ' (' + currentStamps + '/10) descargado. Toca "Abrir" arriba para guardarlo en Wallet.');
            try {
                enableAndroidPush(true);
            } catch (e) {}
        }

        function triggerPkpassDownload() {
            onSaveToWalletClick();
            window.location.href = `/card/${SERIAL}/download.pkpass?v=${Date.now()}`;
        }

        function toggleBackView() {
            showingBack = !showingBack;
            document.getElementById('pass-front-view').style.display = showingBack ? 'none' : 'block';
            document.getElementById('pass-back-view').style.display = showingBack ? 'block' : 'none';
            document.getElementById('btn-flip-toggle').textContent = showingBack ? '← Volver al Frente' : 'ⓘ Detalles / Reverso';
        }

        function showLiveBanner(text) {
            const banner = document.getElementById('live-push-banner');
            document.getElementById('live-push-text').textContent = text;
            banner.classList.add('show');
            try {
                if (navigator.vibrate) navigator.vibrate([120, 60, 180]);
            } catch (e) {}
            setTimeout(() => {
                banner.classList.remove('show');
            }, 6500);
        }

        // 1. Registrar latido / instalación activa en Android automáticamente
        async function registerAndroidHeartbeat() {
            try {
                await fetch(`/api/android/heartbeat/${SERIAL}`, { method: 'POST' });
            } catch (e) {}
        }
        registerAndroidHeartbeat();

        // 2. Sincronización en tiempo real cada 3 segundos (actualiza sellos, strip.png y avisos al instante)
        async function pollCardState() {
            try {
                const res = await fetch(`/api/android/state/${SERIAL}?t=${Date.now()}`, { cache: 'no-store' });
                if (!res.ok) return;
                const data = await res.json();
                if (data.updated_at && data.updated_at !== currentUpdatedAt) {
                    const prevStamps = currentStamps;
                    const prevMsg = currentMessage;
                    const prevTier = currentTier;

                    currentUpdatedAt = data.updated_at;
                    currentStamps = data.stamps;
                    currentMessage = data.message;
                    currentTier = data.tier_name;

                    // Actualizar textos e imagen dinámica strip.png
                    document.getElementById('val-stamps').textContent = `${data.stamps} / 10`;
                    document.getElementById('val-tier').textContent = data.tier_name;
                    document.getElementById('val-reward').textContent = data.reward_status;
                    document.getElementById('val-promo').textContent = data.message;
                    document.getElementById('back-val-msg').textContent = data.message;
                    document.getElementById('back-val-history').textContent = data.history_text;
                    document.getElementById('card-strip-img').src = `/card/${SERIAL}/strip.png?v=${encodeURIComponent(data.updated_at)}`;

                    const saveBtn = document.getElementById('btn-save-wallet-top');
                    if (saveBtn) {
                        saveBtn.href = `/card/${SERIAL}/download.pkpass?v=${encodeURIComponent(data.updated_at)}`;
                    }
                    const saveLbl = document.getElementById('btn-save-wallet-label');
                    if (saveLbl) {
                        saveLbl.textContent = `🔄 Actualizar mi pase en Billetera (${data.stamps}/10 sellos)`;
                    }

                    // Mostrar alerta estilo pantalla bloqueada según el cambio
                    if (data.stamps !== prevStamps) {
                        if (data.stamps >= 10) {
                            showLiveBanner(`🎉 ¡Felicidades! Completaste 10/10 sellos. ¡Tienes 1 Café Gratis disponible!`);
                        } else if (data.stamps === 0) {
                            showLiveBanner(`🎁 ¡Recompensa canjeada! Tu tarjeta se ha reiniciado a 0 / 10.`);
                        } else if (data.stamps < prevStamps) {
                            showLiveBanner(`☕️ Tu tarjeta Maishas Café está al día. Sellos acumulados: ${data.stamps} / 10`);
                        } else {
                            showLiveBanner(`☕️ ¡Has ganado 1 nuevo sello en Maishas Café! Total: ${data.stamps} / 10`);
                        }
                    } else if (data.tier_name !== prevTier) {
                        showLiveBanner(`🏆 ¡Subiste de nivel en Maishas Café! Ahora eres: ${data.tier_name}`);
                    } else if (data.message !== prevMsg) {
                        showLiveBanner(`📣 ${data.message}`);
                    } else {
                        showLiveBanner(`✨ Tu tarjeta Maishas Café se ha actualizado.`);
                    }
                }
            } catch (e) {}
        }
        setInterval(pollCardState, 3000);
        document.addEventListener('visibilitychange', () => {
            if (!document.hidden) pollCardState();
        });

        // Escuchar mensajes en tiempo real desde el Service Worker cuando llega un Push
        if ('serviceWorker' in navigator) {
            navigator.serviceWorker.addEventListener('message', (event) => {
                if (event.data && event.data.type === 'MAISHAS_PUSH') {
                    pollCardState();
                }
            });
        }

        // 3. Web Push (VAPID) + Service Worker para recibir notificaciones incluso con el navegador cerrado
        function urlBase64ToUint8Array(base64String) {
            const padding = '='.repeat((4 - base64String.length % 4) % 4);
            const base64 = (base64String + padding).replace(/\\-/g, '+').replace(/_/g, '/');
            const rawData = window.atob(base64);
            const outputArray = new Uint8Array(rawData.length);
            for (let i = 0; i < rawData.length; ++i) {
                outputArray[i] = rawData.charCodeAt(i);
            }
            return outputArray;
        }

        function markPushActiveUI() {
            const btn = document.getElementById('btn-enable-push');
            const btnTop = document.getElementById('btn-enable-push-top');
            const notice = document.getElementById('push-required-notice');
            if (btn) {
                btn.innerHTML = '<span>✅ Notificaciones Push Activadas en este Android</span>';
                btn.style.background = 'rgba(48, 209, 88, 0.2)';
                btn.style.color = '#4AE072';
                btn.style.borderColor = 'rgba(48, 209, 88, 0.45)';
            }
            if (btnTop) {
                btnTop.innerHTML = '✅ Alertas Push en Vivo Activadas';
                btnTop.style.background = 'rgba(48, 209, 88, 0.2)';
                btnTop.style.color = '#4AE072';
                btnTop.style.borderColor = 'rgba(48, 209, 88, 0.45)';
            }
            if (notice) {
                notice.style.background = 'rgba(48, 209, 88, 0.14)';
                notice.style.borderColor = 'rgba(48, 209, 88, 0.4)';
                notice.style.color = '#66F08B';
                notice.innerHTML = '✅ <strong>Notificaciones Push Activas:</strong> Recibirás alertas en tu celular cuando te agreguen sellos o promociones. Esta tarjeta digital se actualiza sola en tiempo real.';
            }
        }

        async function enableAndroidPush(silentMode) {
            if (!('serviceWorker' in navigator) || !('PushManager' in window)) {
                if (!silentMode) showLiveBanner('✅ Sincronización automática en vivo activada en tu navegador.');
                return false;
            }
            try {
                const perm = await Notification.requestPermission();
                if (perm !== 'granted') {
                    if (!silentMode) {
                        alert('Para recibir alertas cuando te agreguen sellos o te envíen promociones, permite las notificaciones en tu navegador Chrome.');
                    }
                    return false;
                }
                const reg = await navigator.serviceWorker.register('/sw.js');
                await navigator.serviceWorker.ready;
                const sub = await reg.pushManager.subscribe({
                    userVisibleOnly: true,
                    applicationServerKey: urlBase64ToUint8Array(VAPID_PUBLIC_KEY)
                });
                await fetch(`/api/android/subscribe/${SERIAL}`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ subscription: sub.toJSON() })
                });
                markPushActiveUI();
                if (!silentMode) {
                    showLiveBanner('🔔 ¡Notificaciones Push activadas! Recibirás alertas de tus sellos y promociones.');
                }
                return true;
            } catch (err) {
                if (!silentMode) showLiveBanner('✅ Tarjeta vinculada y sincronizada en tiempo real.');
                return false;
            }
        }

        // Si ya dio permiso antes, registrar y sincronizar Service Worker automáticamente al abrir
        if ('serviceWorker' in navigator && 'Notification' in window && Notification.permission === 'granted') {
            enableAndroidPush(true);
        }

        // Si viene desde el botón de actualizar de una notificación Push, descargar el .pkpass actualizado
        (function checkUrlParams() {
            const params = new URLSearchParams(window.location.search);
            if (params.get('auto_pkpass') === '1') {
                setTimeout(() => { triggerPkpassDownload(); }, 400);
            } else if (params.get('from_push') === '1') {
                showLiveBanner(`✨ ¡Tu tarjeta está actualizada! Tienes ${currentStamps} / 10 sellos.`);
            }
        })();

        // 4. Instalación PWA ("Agregar a Pantalla de Inicio" en Android)
        let deferredPrompt = null;
        window.addEventListener('beforeinstallprompt', (e) => {
            e.preventDefault();
            deferredPrompt = e;
        });

        async function installPWA() {
            if ('Notification' in window && Notification.permission !== 'granted') {
                await enableAndroidPush(true);
            }
            if (deferredPrompt) {
                deferredPrompt.prompt();
                await deferredPrompt.userChoice;
                deferredPrompt = null;
            } else {
                alert('📲 Para fijar tu Tarjeta Automática en tu inicio de Android: 1) Toca los 3 puntos (⋮) arriba a la derecha en Chrome. 2) Selecciona «Agregar a la pantalla principal» o «Instalar aplicación». ¡Así tus sellos siempre estarán actualizados en vivo!');
            }
        }
    </script>
</body>
</html>
"""


# ---------------------------------------------------------
# PLANTILLA 3: PANEL DEL BARISTA (APPLE HIG POS — iOS + ANDROID)
# ---------------------------------------------------------
BARISTA_HTML = """
<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
    <title>Maishas Café — Barista POS</title>
    <meta name="theme-color" content="#0D0C0B">
    <meta name="mobile-web-app-capable" content="yes">
    <meta name="apple-mobile-web-app-capable" content="yes">
    <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
    <meta name="apple-mobile-web-app-title" content="Barista POS">
    <link rel="manifest" href="/barista/manifest.webmanifest" crossorigin="use-credentials">
    <link rel="apple-touch-icon" href="/barista/icon-512.png">
    <link rel="icon" type="image/png" href="/card/icon-192.png">
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

        .dynamic-island-wrap {
            position: fixed;
            top: 14px;
            left: 50%;
            transform: translateX(-50%);
            z-index: 9999;
            width: 92%;
            max-width: 640px;
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

        .sticky-header {
            position: sticky;
            top: 0;
            z-index: 100;
            background: rgba(13, 12, 11, 0.78);
            backdrop-filter: blur(24px);
            -webkit-backdrop-filter: blur(24px);
            border-bottom: 1px solid var(--border-subtle);
            padding: calc(12px + env(safe-area-inset-top, 0px)) 24px 12px;
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
        .brand-Cluster { display: flex; align-items: center; gap: 12px; }
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
        .brand-title { font-size: 18px; font-weight: 700; letter-spacing: -0.3px; margin: 0; }
        .brand-sub { font-size: 11px; color: var(--gold-light); font-weight: 600; letter-spacing: 0.4px; text-transform: uppercase; }

        .kpi-cluster { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
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
        .kpi-pill strong { color: var(--text-primary); font-size: 13.5px; font-weight: 700; }
        .kpi-pill.kpi-bday {
            border-color: rgba(255, 55, 95, 0.35);
            background: rgba(255, 55, 95, 0.1);
            color: #FFB3C1;
            cursor: pointer;
        }
        .kpi-pill.kpi-bday strong { color: #FF375F; }

        .spotlight-wrap { position: relative; min-width: 260px; flex: 1; max-width: 340px; }
        .spotlight-input {
            width: 100%;
            padding: 9px 52px 9px 34px;
            border-radius: 10px;
            border: 1px solid var(--border-subtle);
            background: rgba(255, 255, 255, 0.05);
            color: var(--text-primary);
            font-size: 13.5px;
            font-family: inherit;
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

        .main-container { max-width: 1280px; margin: 22px auto 0; padding: 0 24px; }
        .bento-grid {
            display: grid;
            grid-template-columns: 1.05fr 0.95fr 1.15fr;
            gap: 18px;
            margin-bottom: 22px;
        }
        @media (max-width: 1100px) { .bento-grid { grid-template-columns: 1fr 1fr; } }
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
        .bento-header { display: flex; align-items: center; justify-content: space-between; gap: 10px; margin-bottom: 12px; }
        .bento-title { font-size: 15px; font-weight: 700; letter-spacing: -0.2px; margin: 0; display: flex; align-items: center; gap: 8px; }
        .bento-subtitle { font-size: 12px; color: var(--text-secondary); margin: 0 0 12px; line-height: 1.4; }

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
        }
        .seg-btn.active {
            background: rgba(255, 255, 255, 0.12);
            color: var(--text-primary);
            box-shadow: 0 2px 6px rgba(0, 0, 0, 0.3);
        }

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
        #qr-video { width: 100%; max-height: 220px; object-fit: cover; display: none; }
        .qr-idle-placeholder { padding: 24px 16px; text-align: center; }
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
        .quick-scan-form { display: flex; gap: 8px; }
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
        .input-dark:focus { outline: none; border-color: var(--gold); }
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

        .bday-card-widget {
            border-color: rgba(255, 55, 95, 0.22);
            background: linear-gradient(160deg, rgba(32, 20, 24, 0.78) 0%, rgba(22, 20, 18, 0.82) 100%);
        }
        .bday-list { display: flex; flex-direction: column; gap: 8px; max-height: 215px; overflow-y: auto; padding-right: 2px; }
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
        .bday-item.is-today { border-color: rgba(255, 55, 95, 0.45); background: rgba(255, 55, 95, 0.09); }
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
        }

        .chip-row { display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: 8px; }
        .action-chip {
            background: rgba(255, 255, 255, 0.05);
            border: 1px solid var(--border-subtle);
            color: var(--text-secondary);
            border-radius: 999px;
            padding: 5px 10px;
            font-size: 11px;
            font-weight: 600;
            cursor: pointer;
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
        .ios-notif-body { flex: 1; min-width: 0; }
        .ios-notif-top { display: flex; justify-content: space-between; font-size: 10px; font-weight: 700; color: var(--text-secondary); }
        .ios-notif-text { font-size: 12px; color: var(--text-primary); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; margin-top: 1px; }

        .clients-section {
            background: var(--bg-glass);
            backdrop-filter: blur(24px);
            -webkit-backdrop-filter: blur(24px);
            border: 1px solid var(--border-subtle);
            border-radius: 24px;
            padding: 22px;
            box-shadow: 0 14px 40px rgba(0, 0, 0, 0.4);
        }
        .clients-top-bar { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; margin-bottom: 16px; }
        .filter-tabs { display: flex; gap: 6px; flex-wrap: wrap; }
        .filter-pill {
            background: rgba(255, 255, 255, 0.04);
            border: 1px solid var(--border-subtle);
            color: var(--text-secondary);
            padding: 7px 13px;
            border-radius: 999px;
            font-size: 12.5px;
            font-weight: 600;
            cursor: pointer;
        }
        .filter-pill.active {
            background: var(--gold);
            border-color: var(--gold);
            color: #12100E;
            font-weight: 700;
        }

        .client-list { display: flex; flex-direction: column; gap: 10px; }
        .client-card-row {
            background: rgba(255, 255, 255, 0.025);
            border: 1px solid var(--border-subtle);
            border-radius: 16px;
            padding: 14px 16px;
            display: grid;
            grid-template-columns: 1.4fr 1.1fr 0.95fr auto;
            align-items: center;
            gap: 16px;
        }
        .client-card-row.highlight-row {
            background: rgba(48, 209, 88, 0.11);
            border-color: rgba(48, 209, 88, 0.45);
        }
        @media (max-width: 960px) { .client-card-row { grid-template-columns: 1fr; gap: 12px; } }

        .client-identity { display: flex; align-items: flex-start; gap: 12px; min-width: 0; }
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
        .avatar-circle.tier-gold { background: linear-gradient(135deg, #D4AF37, #8F6B1A); color: #12100E; border-color: #F3E5AB; }
        .avatar-circle.tier-silver { background: linear-gradient(135deg, #E0E5EC, #8A95A5); color: #12100E; border-color: #FFF; }
        .client-name-line { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
        .client-name { font-size: 15px; font-weight: 700; color: var(--text-primary); }
        .serial-tag {
            font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
            font-size: 11px;
            color: var(--text-tertiary);
            background: rgba(255, 255, 255, 0.04);
            padding: 2px 6px;
            border-radius: 5px;
        }
        .vip-pill { font-size: 10.5px; font-weight: 700; padding: 2px 8px; border-radius: 99px; display: inline-block; }
        .client-meta { font-size: 12px; color: var(--text-secondary); margin-top: 4px; display: flex; flex-wrap: wrap; gap: 10px; }
        .client-msg-preview { font-size: 11.5px; color: var(--gold-light); margin-top: 4px; font-style: italic; }

        .stamp-progress-col { display: flex; flex-direction: column; gap: 5px; }
        .stamp-top-row { display: flex; align-items: baseline; justify-content: space-between; gap: 8px; }
        .stamp-big-num { font-size: 17px; font-weight: 800; color: var(--gold-light); }
        .stamp-visits-meta { font-size: 11.5px; color: var(--text-secondary); }
        .stamp-dots-row { display: flex; gap: 5px; align-items: center; }
        .s-dot {
            width: 12px;
            height: 12px;
            border-radius: 50%;
            border: 1.5px solid rgba(198, 166, 117, 0.35);
            background: transparent;
            display: inline-block;
        }
        .s-dot.filled { background: var(--gold); border-color: #F3E5AB; box-shadow: 0 0 6px rgba(212, 175, 55, 0.45); }
        .s-dot.full-10 { background: var(--apple-green); border-color: #A8F5BC; box-shadow: 0 0 8px rgba(48, 209, 88, 0.6); }

        .telemetry-col { display: flex; flex-direction: column; gap: 4px; align-items: flex-start; }
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
        .tele-line { font-size: 11px; color: var(--text-secondary); }

        .row-actions { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; justify-content: flex-end; }
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
        }
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
            text-decoration: none;
            display: inline-flex;
            align-items: center;
        }
        .btn-icon-sec:hover { background: rgba(255, 255, 255, 0.1); color: var(--text-primary); }
        .btn-icon-del:hover { background: rgba(255, 55, 95, 0.2); border-color: rgba(255, 55, 95, 0.45); color: #FF6B88; }

        /* Navegación Principal Superior (Mostrador POS vs Base de Datos) */
        .main-nav-tabs {
            display: flex;
            align-items: center;
            gap: 8px;
            flex-wrap: wrap;
        }
        .nav-tab-btn {
            background: rgba(255, 255, 255, 0.05);
            border: 1px solid var(--border-subtle);
            color: var(--text-secondary);
            padding: 9px 15px;
            border-radius: 12px;
            font-size: 13px;
            font-weight: 700;
            cursor: pointer;
            display: inline-flex;
            align-items: center;
            gap: 7px;
            transition: all 0.2s ease;
            text-decoration: none;
        }
        .nav-tab-btn:hover {
            color: var(--text-primary);
            border-color: rgba(212, 175, 55, 0.35);
        }
        .nav-tab-btn.active {
            background: rgba(212, 175, 55, 0.18);
            border-color: rgba(212, 175, 55, 0.5);
            color: var(--gold-light);
            box-shadow: 0 4px 14px rgba(0, 0, 0, 0.35);
        }
        .btn-export-csv {
            background: linear-gradient(135deg, #30D158 0%, #229E42 100%);
            color: #08130B;
            border: 1px solid #98F7AE;
            padding: 9px 15px;
            border-radius: 12px;
            font-size: 13px;
            font-weight: 800;
            cursor: pointer;
            display: inline-flex;
            align-items: center;
            gap: 7px;
            text-decoration: none;
            box-shadow: 0 4px 14px rgba(48, 209, 88, 0.25);
        }

        /* Vista de Tabla Ejecutiva / Base de Datos de Clientes */
        .db-view-section {
            display: none;
        }
        .db-header-card {
            background: var(--bg-card);
            border: 1px solid var(--border-subtle);
            border-radius: 20px;
            padding: 20px 22px;
            margin-bottom: 16px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            flex-wrap: wrap;
            gap: 14px;
        }
        .db-table-wrap {
            background: var(--bg-card);
            border: 1px solid var(--border-subtle);
            border-radius: 20px;
            overflow-x: auto;
            box-shadow: 0 14px 36px rgba(0, 0, 0, 0.45);
        }
        .db-table {
            width: 100%;
            border-collapse: collapse;
            font-size: 13px;
            text-align: left;
            min-width: 980px;
        }
        .db-table th {
            background: rgba(13, 12, 11, 0.85);
            color: var(--gold-light);
            font-size: 11px;
            font-weight: 700;
            text-transform: uppercase;
            letter-spacing: 0.6px;
            padding: 14px 16px;
            border-bottom: 1px solid rgba(212, 175, 55, 0.22);
            white-space: nowrap;
        }
        .db-table td {
            padding: 13px 16px;
            border-bottom: 1px solid rgba(255, 255, 255, 0.06);
            color: var(--text-primary);
            vertical-align: middle;
        }
        .db-table tr:hover td {
            background: rgba(212, 175, 55, 0.05);
        }

        /* Modal Personalizado Glassmorphism (Sin URL de navegador) */
        .custom-modal-backdrop {
            position: fixed;
            inset: 0;
            background: rgba(8, 7, 6, 0.78);
            backdrop-filter: blur(14px);
            -webkit-backdrop-filter: blur(14px);
            z-index: 10000;
            display: none;
            align-items: center;
            justify-content: center;
            padding: 20px;
        }
        .custom-modal-backdrop.open {
            display: flex;
        }
        .custom-modal-box {
            width: 100%;
            max-width: 420px;
            background: linear-gradient(165deg, #1E1B18 0%, #141210 100%);
            border: 1px solid rgba(212, 175, 55, 0.35);
            border-radius: 24px;
            padding: 26px 24px 22px;
            box-shadow: 0 28px 70px rgba(0, 0, 0, 0.85), inset 0 1px 0 rgba(255, 255, 255, 0.12);
            text-align: center;
            animation: modalPop 0.2s cubic-bezier(0.16, 1, 0.3, 1);
        }
        @keyframes modalPop {
            from { transform: scale(0.92); opacity: 0; }
            to { transform: scale(1); opacity: 1; }
        }
        .cm-icon {
            width: 56px;
            height: 56px;
            border-radius: 18px;
            margin: 0 auto 14px;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 26px;
        }
        .cm-icon.danger {
            background: rgba(255, 55, 95, 0.16);
            border: 1px solid rgba(255, 55, 95, 0.4);
        }
        .cm-icon.warn {
            background: rgba(212, 175, 55, 0.16);
            border: 1px solid rgba(212, 175, 55, 0.45);
        }
        .cm-icon.success {
            background: rgba(48, 209, 88, 0.16);
            border: 1px solid rgba(48, 209, 88, 0.45);
        }
        .cm-brand {
            font-size: 11px;
            font-weight: 700;
            letter-spacing: 0.7px;
            text-transform: uppercase;
            color: var(--gold-light);
            margin-bottom: 4px;
        }
        .cm-title {
            font-size: 19px;
            font-weight: 800;
            color: var(--text-primary);
            margin: 0 0 8px;
        }
        .cm-desc {
            font-size: 13.5px;
            color: var(--text-secondary);
            line-height: 1.45;
            margin: 0 0 22px;
        }
        .cm-actions {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 10px;
        }
        .cm-btn-cancel {
            background: rgba(255, 255, 255, 0.07);
            border: 1px solid var(--border-subtle);
            color: var(--text-primary);
            padding: 12px 16px;
            border-radius: 13px;
            font-size: 14px;
            font-weight: 700;
            cursor: pointer;
        }
        .cm-btn-confirm {
            border: none;
            padding: 12px 16px;
            border-radius: 13px;
            font-size: 14px;
            font-weight: 800;
            cursor: pointer;
        }
        .cm-btn-confirm.danger {
            background: linear-gradient(135deg, #FF375F 0%, #C81D40 100%);
            color: #FFFFFF;
        }
        .cm-btn-confirm.warn {
            background: linear-gradient(135deg, #D4AF37 0%, #B38A22 100%);
            color: #12100E;
        }
        .cm-btn-confirm.success {
            background: linear-gradient(135deg, #30D158 0%, #229E42 100%);
            color: #08130B;
        }
    </style>
</head>
<body>

    {% if ok %}
    <div class="dynamic-island-wrap" id="status-toast">
        <div class="dynamic-island">
            <span class="di-dot"></span>
            <span style="flex:1;">{{ ok }}</span>
            <button class="di-close" onclick="document.getElementById('status-toast').style.display='none'">✕</button>
        </div>
    </div>
    {% endif %}

    <!-- MODAL DE CONFIRMACIÓN PERSONALIZADO MAISHAS CAFÉ (Sin alerta del navegador) -->
    <div class="custom-modal-backdrop" id="maishas-confirm-modal">
        <div class="custom-modal-box">
            <div class="cm-icon warn" id="cm-icon-box">⚠️</div>
            <div class="cm-brand">Maishas Café · Confirmación</div>
            <h3 class="cm-title" id="cm-title-text">¿Confirmar acción?</h3>
            <p class="cm-desc" id="cm-desc-text"></p>
            <div class="cm-actions">
                <button type="button" class="cm-btn-cancel" onclick="closeConfirmModal()">Cancelar</button>
                <button type="button" class="cm-btn-confirm warn" id="cm-confirm-btn" onclick="executeConfirmedForm()">Confirmar</button>
            </div>
        </div>
    </div>

    <!-- MODAL DE INSTALACIÓN PWA (iPhone / iPad / Android / Tablet) -->
    <div class="custom-modal-backdrop" id="maishas-pwa-modal">
        <div class="custom-modal-box" style="max-width:450px; text-align:left;">
            <div style="text-align:center;">
                <div class="cm-icon warn" style="font-size:26px;">📲</div>
                <div class="cm-brand">Maishas Café · App Barista POS</div>
                <h3 class="cm-title">Instalar en tu Teléfono o Tablet</h3>
                <p class="cm-desc" style="margin-bottom:14px;">Agrega la consola a tu pantalla de inicio para abrirla a pantalla completa como aplicación nativa:</p>
            </div>
            <div style="background:rgba(255,255,255,0.04); border:1px solid var(--border-subtle); border-radius:14px; padding:12px 14px; margin-bottom:10px; font-size:13px; line-height:1.5; color:var(--text-primary);">
                <strong style="color:var(--gold-light);">🍎 En iPhone o iPad (Safari):</strong><br>
                1. Toca el botón <strong>Compartir</strong> <span style="display:inline-block; padding:1px 6px; border-radius:6px; background:rgba(255,255,255,0.12);">⬆️</span> en la barra de Safari.<br>
                2. Selecciona <strong>«Agregar a pantalla de inicio»</strong> (➕) y toca <strong>Agregar</strong>.
            </div>
            <div style="background:rgba(255,255,255,0.04); border:1px solid var(--border-subtle); border-radius:14px; padding:12px 14px; margin-bottom:18px; font-size:13px; line-height:1.5; color:var(--text-primary);">
                <strong style="color:#30D158;">🤖 En Android o Tablet (Chrome):</strong><br>
                1. Toca los <strong>3 puntitos (⋮)</strong> arriba a la derecha en Chrome.<br>
                2. Toca <strong>«Instalar aplicación»</strong> o <strong>«Agregar a la pantalla principal»</strong>.
            </div>
            <div class="cm-actions" style="grid-template-columns:1fr;">
                <button type="button" class="cm-btn-confirm warn" onclick="document.getElementById('maishas-pwa-modal').classList.remove('open')">Entendido</button>
            </div>
        </div>
    </div>

    <header class="sticky-header">
        <div class="header-inner">
            <div class="brand-Cluster">
                <div class="brand-emblem">☕️</div>
                <div>
                    <h1 class="brand-title">Maishas Café</h1>
                    <span class="brand-sub">Navojoa, Sonora · Consola Barista</span>
                </div>
            </div>

            <!-- Pestañas de Vista Principal (Mostrador vs Base de Datos y Machote Excel) -->
            <div class="main-nav-tabs">
                <button type="button" id="nav-btn-pos" class="nav-tab-btn active" onclick="switchMainView('pos')">
                    ☕️ Mostrador POS
                </button>
                <button type="button" id="nav-btn-db" class="nav-tab-btn" onclick="switchMainView('db')">
                    📊 Base de Datos de Clientes ({{ clients|length }})
                </button>
                <a href="/api/export-clients.csv" class="btn-export-csv" title="Descargar machote de clientes en formato Excel (.CSV)">
                    📥 Descargar Machote Excel
                </a>
                <button type="button" id="btn-install-barista-pwa" class="nav-tab-btn" onclick="installBaristaPwa()" style="border-color:rgba(212,175,55,0.45); color:var(--gold-light);">
                    📲 Instalar App
                </button>
            </div>

            <div class="spotlight-wrap">
                <span class="spotlight-icon">🔍</span>
                <input type="text" id="client-search" class="spotlight-input" placeholder="Buscar nombre, código, celular..." oninput="filterClients()">
                <span class="spotlight-kbd">⌘K</span>
            </div>
        </div>
    </header>

    <main class="main-container">
        <!-- VISTA 1: OPERACIÓN EN MOSTRADOR POS -->
        <div id="view-pos-container">
        <section class="bento-grid">

            <!-- MÓDULO 1: ESCÁNER QR CON HUD ESTILO CÁMARA iOS -->
            <div class="bento-card">
                <div>
                    <div class="bento-header">
                        <h2 class="bento-title">📷 Escáner QR Mostrador</h2>
                        <div class="segmented-control">
                            <button type="button" id="seg-auto-on" class="seg-btn active" onclick="setAutoScanMode(true)">⚡ +1 Auto</button>
                            <button type="button" id="seg-auto-off" class="seg-btn" onclick="setAutoScanMode(false)">🔍 Manual</button>
                        </div>
                    </div>
                    <p class="bento-subtitle">Escanea la tarjeta del cliente (iPhone o Android).</p>

                    <div class="qr-hud-wrapper" id="qr-hud-box">
                        <div class="scan-flash" id="scan-flash-overlay"></div>
                        <video id="qr-video" playsinline muted></video>
                        <div class="qr-reticle" id="qr-reticle-guide"></div>
                        <canvas id="qr-canvas" style="display:none;"></canvas>

                        <div class="qr-idle-placeholder" id="qr-idle-view">
                            <button type="button" id="btn-toggle-cam" class="btn-cam-launch" onclick="toggleScanner()">
                                <span>📷 Activar Cámara QR</span>
                            </button>
                            <div style="font-size:11px; color:var(--text-tertiary); margin-top:8px;">Compatible con iPhone, Android, iPad y Mac</div>
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

            <!-- MÓDULO 2: 🎂 RADAR DE CUMPLEAÑOS -->
            <div class="bento-card bday-card-widget">
                <div>
                    <div class="bento-header">
                        <h2 class="bento-title">🎂 Radar de Cumpleaños</h2>
                        <span style="font-size:11px; font-weight:700; color:#FF6B88; background:rgba(255,55,95,0.14); padding:4px 9px; border-radius:99px;">
                            Hoy y próx. 7 días
                        </span>
                    </div>
                    <p class="bday-subtitle bento-subtitle">Envía una felicitación o regalo directo a su celular con 1 clic.</p>

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
                                <button type="submit" class="btn-bday-gift">
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

            <!-- MÓDULO 3: MARKETING PUSH & GEOLOCALIZACIÓN GPS -->
            <div class="bento-card">
                <div>
                    <div class="bento-header">
                        <h2 class="bento-title" id="mkt-card-title">📣 Marketing Push</h2>
                        <div class="segmented-control">
                            <button type="button" id="tab-btn-push" class="seg-btn active" onclick="switchMktTab('push')">📣 Push</button>
                            <button type="button" id="tab-btn-gps" class="seg-btn" onclick="switchMktTab('gps')">📍 GPS ({{ "%.2f"|format(shop_loc.lat) }})</button>
                        </div>
                    </div>

                    <div id="mkt-pane-push">
                        <div class="chip-row">
                            <button type="button" class="action-chip" onclick="setTemplate('🥐 ¡Recién salidos del horno! Ven por tu pan calientito y suma tu sello de hoy ☕️')">🥐 Pan horneado</button>
                            <button type="button" class="action-chip" onclick="setTemplate('✨ ¡Hora Feliz en Maishas Café! 2x1 en bebidas seleccionadas hoy de 4 a 6 pm ☕️')">✨ Hora Feliz 2x1</button>
                            <button type="button" class="action-chip" onclick="setTemplate('🔥 ¡Hoy tus compras valen DOBLE sello en Maishas Café! Te esperamos ☕️')">🔥 Doble Sello</button>
                        </div>

                        <form action="/api/send-message" method="POST">
                            <textarea name="message" id="push-msg-box" class="input-dark" maxlength="200" rows="2" placeholder="Escribe una promoción o aviso para tus clientes..." required oninput="updateLockScreenPreview(this.value)"></textarea>

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
                                    <option value="all">📲 Todos ({{ installed_count }} teléfonos activos)</option>
                                    {% for client in clients %}
                                    <option value="{{ client.serial }}">{{ client.name }} ({{ client.serial }})</option>
                                    {% endfor %}
                                </select>
                                <button type="submit" class="btn-cam-launch" style="white-space:nowrap;">Enviar Push 📲</button>
                            </div>
                        </form>
                    </div>

                    <div id="mkt-pane-gps" style="display:none;">
                        <p class="bento-subtitle">Alerta automática cuando el cliente pasa a ~150m de la cafetería.</p>
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
                                    <button type="button" class="btn-gold-sm" onclick="detectMyGPS()">📍 Mi GPS</button>
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

        <!-- LISTA OPERATIVA DE CLIENTES -->
        <section class="clients-section">
            <div class="clients-top-bar">
                <div class="filter-tabs" id="filter-tabs-bar">
                    <button type="button" class="filter-pill active" data-tab="all" onclick="setFilterTab('all')">Todos ({{ clients|length }})</button>
                    <button type="button" class="filter-pill" data-tab="installed" onclick="setFilterTab('installed')">📲 Activos ({{ installed_count }})</button>
                    <button type="button" class="filter-pill" data-tab="ready" onclick="setFilterTab('ready')">🎁 Listos para Canjear ({{ ready_to_redeem_count }})</button>
                    <button type="button" class="filter-pill" data-tab="bday" onclick="setFilterTab('bday')">🎂 Cumpleaños ({{ upcoming_birthdays|length + month_birthdays_count }})</button>
                    <button type="button" class="filter-pill" data-tab="vip" onclick="setFilterTab('vip')">♛ Socios VIP ({{ gold_count + silver_count }})</button>
                </div>
            </div>

            <div class="client-list" id="clients-list-container">
                {% for client in clients %}
                <div id="row-{{ client.serial }}"
                     class="client-card-row client-row {{ 'highlight-row' if updated_serial == client.serial else '' }}"
                     data-search="{{ (client.name ~ ' ' ~ client.serial ~ ' ' ~ client.email ~ ' ' ~ client.phone ~ ' ' ~ client.tier.badge ~ ' ' ~ client.created_at)|lower }}"
                     data-installed="{{ '1' if client.is_installed else '0' }}"
                     data-ready="{{ '1' if client.stamps >= 10 else '0' }}"
                     data-bday="{{ '1' if (client.bday.is_upcoming_week or client.bday.is_this_month) else '0' }}"
                     data-vip="{{ '1' if client.total_visits >= 10 else '0' }}">

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
                                {% if client.created_at %}<span>🗓️ Alta: {{ client.created_at }}</span>{% endif %}
                            </div>
                            {% if client.message %}
                            <div class="client-msg-preview">💬 "{{ client.message }}"</div>
                            {% endif %}
                        </div>
                    </div>

                    <div class="stamp-progress-col">
                        <div class="stamp-top-row">
                            <span class="stamp-big-num">{{ client.stamps }} / 10</span>
                            <span class="stamp-visits-meta">
                                ☕️ {{ client.total_visits }} visitas
                                {% if client.redeemed_count > 0 %} · 🎁 {{ client.redeemed_count }} canjes{% endif %}
                            </span>
                        </div>
                        <div class="stamp-dots-row">
                            {% for idx in range(10) %}
                            <span class="s-dot {{ 'full-10' if client.stamps >= 10 else ('filled' if idx < client.stamps else '') }}"></span>
                            {% endfor %}
                        </div>
                    </div>

                    <div class="telemetry-col">
                        {% if client.ios_installed and client.android_installed %}
                        <span class="status-pill-on">📲 Activo en iPhone y Android</span>
                        {% elif client.ios_installed %}
                        <span class="status-pill-on"> Activo en iPhone</span>
                        {% elif client.android_installed %}
                        <span class="status-pill-on">🤖 Activo en Android</span>
                        {% else %}
                        <span class="status-pill-off">⏳ Aún no instala su tarjeta</span>
                        {% endif %}
                        {% if client.last_push_clean %}
                        <span class="tele-line">{{ client.last_push_clean }}</span>
                        {% endif %}
                        {% if client.last_sync_clean %}
                        <span class="tele-line" style="color:#7CE8B5;">🔄 Hora local Sonora: {{ client.last_sync_clean }}</span>
                        {% endif %}
                    </div>

                    <div class="row-actions">
                        <form action="/api/add-stamp" method="POST" style="margin:0;">
                            <input type="hidden" name="serial" value="{{ client.serial }}">
                            <input type="hidden" name="action" value="add">
                            <button type="submit" class="btn-stamp-plus">+1 Sello ☕️</button>
                        </form>

                        {% if client.stamps >= 10 %}
                        <form id="form-redeem-{{ client.serial }}" action="/api/add-stamp" method="POST" style="margin:0;">
                            <input type="hidden" name="serial" value="{{ client.serial }}">
                            <input type="hidden" name="action" value="reset">
                            <button type="button" class="btn-redeem-glow"
                                    onclick="openConfirmModal('redeem', 'form-redeem-{{ client.serial }}', '{{ client.name|e }}', '{{ client.serial }}', {{ client.stamps }})">
                                🎁 Canjear Café
                            </button>
                        </form>
                        {% elif client.stamps > 0 %}
                        <form id="form-redeem-{{ client.serial }}" action="/api/add-stamp" method="POST" style="margin:0;">
                            <input type="hidden" name="serial" value="{{ client.serial }}">
                            <input type="hidden" name="action" value="reset">
                            <button type="button" class="btn-redeem-normal"
                                    onclick="openConfirmModal('redeem', 'form-redeem-{{ client.serial }}', '{{ client.name|e }}', '{{ client.serial }}', {{ client.stamps }})">
                                🎁 Canjear
                            </button>
                        </form>
                        {% endif %}

                        {% if client.stamps > 0 %}
                        <form id="form-sub-{{ client.serial }}" action="/api/add-stamp" method="POST" style="margin:0;">
                            <input type="hidden" name="serial" value="{{ client.serial }}">
                            <input type="hidden" name="action" value="sub">
                            <button type="button" class="btn-icon-sec" title="Restar 1 sello"
                                    onclick="openConfirmModal('sub', 'form-sub-{{ client.serial }}', '{{ client.name|e }}', '{{ client.serial }}', {{ client.stamps }})">
                                -1
                            </button>
                        </form>
                        {% endif %}

                        <a href="/card/{{ client.serial }}" target="_blank" class="btn-icon-sec" title="Abrir Tarjeta Digital de {{ client.name }}">📲</a>
                        <button type="button" class="btn-icon-sec" title="Enviar mensaje directo a {{ client.name }}" onclick="selectClientForPush('{{ client.serial }}', '{{ client.name|e }}')">💬</button>

                        <form id="form-del-{{ client.serial }}" action="/api/delete-client" method="POST" style="margin:0;">
                            <input type="hidden" name="serial" value="{{ client.serial }}">
                            <button type="button" class="btn-icon-sec btn-icon-del" title="Eliminar registro"
                                    onclick="openConfirmModal('delete', 'form-del-{{ client.serial }}', '{{ client.name|e }}', '{{ client.serial }}', {{ client.stamps }})">
                                🗑️
                            </button>
                        </form>
                    </div>
                </div>
                {% else %}
                <div style="text-align:center; padding:40px 20px; color:var(--text-secondary);">
                    No hay clientes registrados todavía.
                </div>
                {% endfor %}
            </div>
        </section>
        </div>

        <!-- VISTA 2: BASE DE DATOS EJECUTIVA Y DESCARGA DE MACHOTE EXCEL (.CSV) -->
        <section id="view-db-container" class="db-view-section">
            <div class="db-header-card">
                <div>
                    <h2 style="margin:0; font-size:20px; font-weight:800; color:var(--text-primary);">
                        📊 Base de Datos General de Clientes
                    </h2>
                    <p style="margin:4px 0 0; font-size:13px; color:var(--text-secondary);">
                        Consulta todos los datos capturados, fecha de alta en hora local de Navojoa, Sonora, historial de visitas y exporta tu machote para Excel.
                    </p>
                </div>
                <div style="display:flex; gap:10px; align-items:center; flex-wrap:wrap;">
                    <a href="/api/export-clients.csv" class="btn-export-csv">
                        📥 Descargar Machote de Clientes (.CSV para Excel)
                    </a>
                </div>
            </div>

            <div class="db-table-wrap">
                <table class="db-table">
                    <thead>
                        <tr>
                            <th>Código Serial</th>
                            <th>Nombre del Cliente</th>
                            <th>Celular</th>
                            <th>Correo Electrónico</th>
                            <th>Fecha Nacimiento</th>
                            <th>Fecha de Alta (Sonora)</th>
                            <th>Teléfono / Estado</th>
                            <th>Sellos</th>
                            <th>Visitas Totales</th>
                            <th>Cafés Canjeados</th>
                            <th>Nivel VIP</th>
                            <th>Última Actividad</th>
                        </tr>
                    </thead>
                    <tbody>
                        {% for client in clients %}
                        <tr class="db-client-row"
                            data-search="{{ (client.name ~ ' ' ~ client.serial ~ ' ' ~ client.email ~ ' ' ~ client.phone ~ ' ' ~ client.tier.badge ~ ' ' ~ client.created_at)|lower }}">
                            <td><span class="serial-tag">{{ client.serial }}</span></td>
                            <td><strong>{{ client.name }}</strong></td>
                            <td>{{ client.phone if client.phone else '—' }}</td>
                            <td>{{ client.email if client.email else '—' }}</td>
                            <td>
                                {% if client.birthdate %}
                                    {{ client.birthdate }} ({{ client.bday.short_date }})
                                {% else %}
                                    —
                                {% endif %}
                            </td>
                            <td>{{ client.created_at if client.created_at else '—' }}</td>
                            <td>
                                {% if client.ios_installed and client.android_installed %}
                                    <span class="status-pill-on"> iPhone + 🤖 Android</span>
                                {% elif client.ios_installed %}
                                    <span class="status-pill-on"> iPhone</span>
                                {% elif client.android_installed %}
                                    <span class="status-pill-on">🤖 Android</span>
                                {% else %}
                                    <span class="status-pill-off">⏳ Pendiente</span>
                                {% endif %}
                            </td>
                            <td><strong style="color:var(--gold-light);">{{ client.stamps }} / 10</strong></td>
                            <td>{{ client.total_visits }}</td>
                            <td>{{ client.redeemed_count }}</td>
                            <td>
                                <span class="vip-pill" style="background:{{ client.tier.bg }}; color:{{ client.tier.fg }}; border:1px solid {{ client.tier.border }};">
                                    {{ client.tier.badge }}
                                </span>
                            </td>
                            <td style="font-size:12px; color:var(--text-secondary);">
                                {{ client.last_sync_clean if client.last_sync_clean else (client.last_push_clean if client.last_push_clean else '—') }}
                            </td>
                        </tr>
                        {% else %}
                        <tr>
                            <td colspan="12" style="text-align:center; padding:32px; color:var(--text-secondary);">
                                No hay clientes registrados todavía.
                            </td>
                        </tr>
                        {% endfor %}
                    </tbody>
                </table>
            </div>
        </section>
    </main>

    <script>
        if (window.history.replaceState && window.location.search) {
            window.history.replaceState({}, document.title, window.location.pathname);
        }

        const toastEl = document.getElementById('status-toast');
        if (toastEl) {
            setTimeout(() => {
                toastEl.style.transition = 'opacity 0.4s ease';
                toastEl.style.opacity = '0';
                setTimeout(() => toastEl.style.display = 'none', 400);
            }, 6500);
        }

        // Cambio entre Vista Mostrador POS y Vista Base de Datos / Excel
        function switchMainView(view) {
            const posContainer = document.getElementById('view-pos-container');
            const dbContainer = document.getElementById('view-db-container');
            const btnPos = document.getElementById('nav-btn-pos');
            const btnDb = document.getElementById('nav-btn-db');
            if (view === 'db') {
                posContainer.style.display = 'none';
                dbContainer.style.display = 'block';
                btnPos.classList.remove('active');
                btnDb.classList.add('active');
            } else {
                posContainer.style.display = 'block';
                dbContainer.style.display = 'none';
                btnPos.classList.add('active');
                btnDb.classList.remove('active');
            }
        }

        // Modal de Confirmación Personalizado (Sin URL de navegador)
        let pendingFormId = null;
        function openConfirmModal(actionType, formId, clientName, serial, currentStamps) {
            pendingFormId = formId;
            const modal = document.getElementById('maishas-confirm-modal');
            const iconBox = document.getElementById('cm-icon-box');
            const titleEl = document.getElementById('cm-title-text');
            const descEl = document.getElementById('cm-desc-text');
            const confirmBtn = document.getElementById('cm-confirm-btn');

            if (actionType === 'delete') {
                iconBox.className = 'cm-icon danger';
                iconBox.textContent = '🗑️';
                titleEl.textContent = '¿Eliminar cliente?';
                descEl.innerHTML = 'Estás por eliminar permanentemente a <strong>' + clientName + '</strong> (' + serial + '). Esta acción no se puede deshacer.';
                confirmBtn.className = 'cm-btn-confirm danger';
                confirmBtn.textContent = 'Sí, eliminar';
            } else if (actionType === 'sub') {
                const nextStamps = Math.max(0, currentStamps - 1);
                iconBox.className = 'cm-icon warn';
                iconBox.textContent = '☕️';
                titleEl.textContent = '¿Corregir sellos (-1)?';
                descEl.innerHTML = '¿Estás seguro de quitar 1 sello a <strong>' + clientName + '</strong>? Su tarjeta pasará de <strong>' + currentStamps + ' / 10</strong> a <strong>' + nextStamps + ' / 10</strong> sellos.';
                confirmBtn.className = 'cm-btn-confirm warn';
                confirmBtn.textContent = 'Sí, restar 1 sello';
            } else if (actionType === 'redeem') {
                iconBox.className = 'cm-icon success';
                iconBox.textContent = '🎁';
                titleEl.textContent = '¿Canjear Café Gratis?';
                descEl.innerHTML = 'Se entregará 1 Café Gratis a <strong>' + clientName + '</strong> y su tarjeta se reiniciará a <strong>0 / 10</strong> sellos.';
                confirmBtn.className = 'cm-btn-confirm success';
                confirmBtn.textContent = 'Sí, canjear café';
            }

            modal.classList.add('open');
        }

        function closeConfirmModal() {
            pendingFormId = null;
            document.getElementById('maishas-confirm-modal').classList.remove('open');
        }

        function executeConfirmedForm() {
            if (pendingFormId) {
                const form = document.getElementById(pendingFormId);
                if (form) form.submit();
            }
            closeConfirmModal();
        }

        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape') {
                closeConfirmModal();
            }
            if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
                e.preventDefault();
                const searchInput = document.getElementById('client-search');
                if (searchInput) {
                    searchInput.focus();
                    searchInput.select();
                }
            }
        });

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
            switchMainView('pos');
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

            const dbRows = document.querySelectorAll('.db-client-row');
            dbRows.forEach(row => {
                const searchTxt = row.getAttribute('data-search') || '';
                row.style.display = (!q || searchTxt.includes(q)) ? '' : 'none';
            });
        }

        function detectMyGPS() {
            if (!navigator.geolocation) {
                return;
            }
            navigator.geolocation.getCurrentPosition(
                pos => {
                    document.getElementById('lat-input').value = pos.coords.latitude.toFixed(6);
                    document.getElementById('lng-input').value = pos.coords.longitude.toFixed(6);
                },
                () => {},
                { enableHighAccuracy: true }
            );
        }

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
                status.textContent = 'Apunta al código QR del cliente (Clic en el video para cerrar)';
                video.onclick = stopScanner;
                requestAnimationFrame(scanTick);
            } catch (err) {
                status.style.display = 'block';
                status.textContent = '⚠️ No se pudo abrir la cámara: ' + err.message;
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

        // --- PWA Instalación en Teléfono / Tablet / iPad ---
        let deferredBaristaPrompt = null;
        const installBtn = document.getElementById('btn-install-barista-pwa');

        function checkStandaloneMode() {
            const isStandalone = window.matchMedia('(display-mode: standalone)').matches || window.navigator.standalone === true;
            if (isStandalone && installBtn) {
                installBtn.style.display = 'none';
            }
        }
        checkStandaloneMode();

        if ('serviceWorker' in navigator) {
            navigator.serviceWorker.register('/sw.js').catch(() => {});
        }

        window.addEventListener('beforeinstallprompt', (e) => {
            e.preventDefault();
            deferredBaristaPrompt = e;
            if (installBtn) installBtn.style.display = 'inline-flex';
        });

        window.addEventListener('appinstalled', () => {
            deferredBaristaPrompt = null;
            if (installBtn) installBtn.style.display = 'none';
        });

        async function installBaristaPwa() {
            if (deferredBaristaPrompt) {
                deferredBaristaPrompt.prompt();
                try { await deferredBaristaPrompt.userChoice; } catch (e) {}
                deferredBaristaPrompt = null;
            } else {
                document.getElementById('maishas-pwa-modal').classList.add('open');
            }
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
    redeemed_count=0,
    last_action="add",
    for_android=False
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
        clean_visible_msg = (message or DEFAULT_MESSAGE).replace("\u200b", "")

        render_dynamic_strip(stamps, temp_dir, total_visits=visits, message=clean_visible_msg)

        pass_json_path = os.path.join(temp_dir, "pass.json")
        with open(pass_json_path, "r", encoding="utf-8") as f:
            pass_data = json.load(f)

        pass_data["serialNumber"] = serial_number
        pass_data["foregroundColor"] = "rgb(245, 242, 235)"
        pass_data["backgroundColor"] = "rgb(18, 16, 14)"
        pass_data["labelColor"] = "rgb(198, 166, 117)"
        pass_data["logoText"] = "MAISHAS CAFÉ"

        barcode_obj = {
            "format": "PKBarcodeFormatQR",
            "message": serial_number,
            "messageEncoding": "utf-8",
            "altText": serial_number
        }
        pass_data["barcode"] = barcode_obj
        pass_data["barcodes"] = [barcode_obj]

        # Mensaje de notificación inteligente según la acción realizada (evita decir 'ganaste 1 sello' cuando se restó -1)
        if last_action == "sub":
            stamp_change_msg = "☕️ Tu tarjeta Maishas Café está al día. Sellos acumulados: %@"
        elif last_action == "reset" or stamps == 0:
            stamp_change_msg = "🎁 ¡Disfruta tu Café Gratis! Tu tarjeta Maishas Café está en: %@"
        elif stamps >= 10:
            stamp_change_msg = "🎉 ¡Felicidades! Completaste %@ sellos. ¡Tienes 1 Café Gratis en Maishas Café!"
        else:
            stamp_change_msg = "☕️ ¡Has ganado 1 nuevo sello en Maishas Café! Total: %@"

        pass_data["storeCard"]["headerFields"] = [
            {
                "key": "stamps",
                "label": "SELLOS",
                "value": f"{stamps} / 10",
                "textAlignment": "PKTextAlignmentRight",
                "changeMessage": stamp_change_msg
            }
        ]

        reward_status = "¡CAFÉ GRATIS! 🎉" if stamps >= 10 else f"Faltan {10 - stamps} sellos"

        if for_android:
            # Google Wallet y lectores .pkpass de Android requieren primaryFields y auxiliaryFields no vacíos
            pass_data["storeCard"]["primaryFields"] = [
                {
                    "key": "points",
                    "label": "SELLOS ACUMULADOS",
                    "value": f"{stamps} / 10"
                }
            ]
            pass_data["storeCard"]["secondaryFields"] = [
                {
                    "key": "memberName",
                    "label": "CLIENTE",
                    "value": client_name
                },
                {
                    "key": "vipTier",
                    "label": "NIVEL VIP",
                    "value": tier["short"]
                }
            ]
            pass_data["storeCard"]["auxiliaryFields"] = [
                {
                    "key": "serialCode",
                    "label": "CÓDIGO ID",
                    "value": serial_number
                },
                {
                    "key": "promo",
                    "label": f"AVISO · {reward_status.upper()}",
                    "value": clean_visible_msg
                }
            ]
        else:
            # En iPhone (iOS Apple Wallet), primaryFields vacío evita texto sobre el strip.png
            pass_data["storeCard"]["primaryFields"] = []
            pass_data["storeCard"]["secondaryFields"] = [
                {
                    "key": "memberName",
                    "label": f"CLIENTE · {tier['short'].upper()}",
                    "value": client_name
                },
                {
                    "key": "promo",
                    "label": f"AVISO · {reward_status.upper()}",
                    "value": clean_visible_msg,
                    "textAlignment": "PKTextAlignmentRight"
                }
            ]
            pass_data["storeCard"]["auxiliaryFields"] = []

        shop_loc = get_shop_location()
        pass_data["locations"] = [
            {
                "latitude": shop_loc["lat"],
                "longitude": shop_loc["lng"],
                "relevantText": shop_loc["relevant_text"]
            }
        ]
        pass_data["maxDistance"] = 150

        clean_host = host_url.rstrip("/")
        if not ("localhost" in clean_host or "127.0.0.1" in clean_host):
            clean_host = clean_host.replace("http://", "https://", 1)
        pass_data["webServiceURL"] = f"{clean_host}/"
        pass_data["authenticationToken"] = AUTH_TOKEN

        pass_data["storeCard"]["backFields"].insert(0, {
            "key": "memberSerialId",
            "label": "Código Único de Socio (ID)",
            "value": serial_number
        })

        pass_data["storeCard"]["backFields"].insert(1, {
            "key": "news",
            "label": "Mensaje de Maishas Café",
            "value": message or DEFAULT_MESSAGE,
            "changeMessage": "📣 %@"
        })

        pass_data["storeCard"]["backFields"].insert(2, {
            "key": "vipHistory",
            "label": "Historial y Nivel de Socio",
            "value": f"Nivel: {tier['name']} | Visitas históricas: {visits} | Cafés gratis canjeados: {redeemed_count} ({tier['next_goal']})"
        })

        pass_data["storeCard"]["backFields"].insert(3, {
            "key": "liveCardLink",
            "label": "Tarjeta Digital en Vivo (Sellos y Notificaciones)",
            "value": f"{clean_host}/card/{serial_number}"
        })

        extra_info = []
        if phone:
            extra_info.append(f"Celular: {phone}")
        if birthdate:
            extra_info.append(f"Cumpleaños: {birthdate}")
        if extra_info:
            pass_data["storeCard"]["backFields"].insert(4, {
                "key": "memberDetails",
                "label": "Datos del Cliente",
                "value": " | ".join(extra_info)
            })

        with open(pass_json_path, "w", encoding="utf-8") as f:
            json.dump(pass_data, f, indent=2)

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

        sig_path = os.path.join(temp_dir, "signature")
        cmd_sign = (
            f'openssl smime -sign -signer "{CERT_PEM}" -inkey "{KEY_PEM}" '
            f'-certfile "{WWDR_PEM}" -in "{manifest_path}" -out "{sig_path}" '
            f'-outform DER -binary'
        )
        subprocess.run(cmd_sign, shell=True, check=True)

        suffix = "_android" if for_android else "_ios"
        output_pkpass = f"/tmp/{serial_number}{suffix}.pkpass"
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
# RUTA PRINCIPAL, BARISTA, EXPORTACIÓN EXCEL Y TARJETA ANDROID
# ---------------------------------------------------------

@app.route("/")
def index():
    return render_template_string(REGISTRATION_HTML)


@app.route("/barista")
@requires_auth
def barista():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        SELECT c.serial, c.name, c.email, c.stamps, c.push_token, c.message,
               c.phone, c.birthdate, c.last_push_status, c.last_sync_at,
               (SELECT COUNT(*) FROM registrations r WHERE r.serial = c.serial) as reg_count,
               c.total_visits, c.redeemed_count, c.android_active,
               (SELECT COUNT(*) FROM android_subscriptions a WHERE a.serial = c.serial) as and_sub_count,
               c.created_at
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
        ios_installed = bool(r[4]) or reg_count > 0
        android_installed = bool(r[13]) or ((r[14] or 0) > 0)
        is_installed = ios_installed or android_installed
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

        created_at_clean = format_sonora_time(r[15] or "")
        last_push_clean = humanize_push_status(r[8] or "")
        last_sync_clean = format_sonora_time(r[9] or "")

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
            "last_push_clean": last_push_clean,
            "last_sync_at": r[9] or "",
            "last_sync_clean": last_sync_clean,
            "created_at": created_at_clean,
            "ios_count": max(reg_count, 1 if r[4] else 0),
            "ios_installed": ios_installed,
            "android_installed": android_installed,
            "is_installed": is_installed,
            "total_visits": visits_val,
            "redeemed_count": redeemed_val,
            "tier": tier,
        }
        clients.append(client_obj)
        if bday_info["is_upcoming_week"]:
            upcoming_birthdays.append(client_obj)

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


@app.route("/api/export-clients.csv")
@requires_auth
def export_clients_csv():
    """Descarga el machote completo de clientes en formato CSV compatible con Microsoft Excel (UTF-8 con BOM)."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        SELECT c.serial, c.name, c.email, c.phone, c.birthdate, c.created_at,
               c.stamps, c.total_visits, c.redeemed_count, c.message,
               c.last_sync_at, c.last_push_status, c.push_token, c.android_active,
               (SELECT COUNT(*) FROM registrations r WHERE r.serial = c.serial) as reg_count,
               (SELECT COUNT(*) FROM android_subscriptions a WHERE a.serial = c.serial) as and_sub_count
        FROM clients c
        ORDER BY c.rowid DESC
    """)
    rows = c.fetchall()
    conn.close()

    output = io.StringIO()
    # Escribir BOM UTF-8 para que Microsoft Excel reconozca acentos y eñes automáticamente
    output.write("\ufeff")
    writer = csv.writer(output)
    writer.writerow([
        "Código Serial",
        "Nombre del Cliente",
        "Número de Celular",
        "Correo Electrónico",
        "Fecha de Nacimiento",
        "Fecha de Alta (Sonora)",
        "Dispositivo Activo",
        "Sellos Actuales (de 10)",
        "Visitas Históricas",
        "Cafés Gratis Canjeados",
        "Nivel VIP",
        "Último Aviso en Tarjeta",
        "Última Actividad (Sonora)"
    ])

    for r in rows:
        serial, name, email, phone, bdate, created_at, stamps, visits, redeemed, msg, sync_at, push_st, p_tok, and_act, reg_c, and_c = r
        ios_on = bool(p_tok) or ((reg_c or 0) > 0)
        and_on = bool(and_act) or ((and_c or 0) > 0)
        if ios_on and and_on:
            dev_label = "iPhone + Android"
        elif ios_on:
            dev_label = "iPhone (Apple Wallet)"
        elif and_on:
            dev_label = "Android Wallet"
        else:
            dev_label = "Pendiente de instalar"

        stamps_v = stamps or 0
        visits_v = visits if visits is not None else stamps_v
        redeemed_v = redeemed or 0
        tier_name = get_vip_tier(visits_v)["name"]
        clean_msg = (msg or DEFAULT_MESSAGE).replace("\u200b", "")
        created_clean = format_sonora_time(created_at or "")
        act_clean = format_sonora_time(sync_at or "") or humanize_push_status(push_st or "")

        writer.writerow([
            serial or "",
            name or "",
            phone or "",
            email or "",
            bdate or "",
            created_clean,
            dev_label,
            stamps_v,
            visits_v,
            redeemed_v,
            tier_name,
            clean_msg,
            act_clean
        ])

    csv_bytes = output.getvalue().encode("utf-8")
    now_str = datetime.now(SONORA_TZ).strftime("%Y-%m-%d")
    return Response(
        csv_bytes,
        mimetype="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="Clientes_MaishasCafe_{now_str}.csv"'
        }
    )


@app.route("/api/register", methods=["POST"])
def register():
    name = (request.form.get("name") or "").strip()
    email = (request.form.get("email") or "").strip()
    phone = (request.form.get("phone") or "").strip()
    birthdate = (request.form.get("birthdate") or "").strip()
    platform_req = (request.form.get("platform") or "").strip().lower()

    ua = (request.headers.get("User-Agent") or "").lower()
    ch_plat = (request.headers.get("Sec-CH-UA-Platform") or "").lower()
    if platform_req == "android" or "android" in ua or "android" in ch_plat:
        platform = "android"
    else:
        platform = "ios"

    now_ts = get_utc_timestamp()
    created_sonora = get_sonora_datetime_str()
    android_active = 1 if platform == "android" else 0

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # Evitar duplicados: si el cliente ya se registró con el mismo correo electrónico, reutilizar su mismo ID MAISHAS-XXXXXX
    existing = None
    if email:
        existing = c.execute(
            "SELECT serial, stamps, total_visits, redeemed_count, message, phone, birthdate FROM clients WHERE LOWER(TRIM(email)) = LOWER(?) ORDER BY rowid DESC LIMIT 1",
            (email,)
        ).fetchone()
    elif name:
        existing = c.execute(
            "SELECT serial, stamps, total_visits, redeemed_count, message, phone, birthdate FROM clients WHERE LOWER(TRIM(name)) = LOWER(?) ORDER BY rowid DESC LIMIT 1",
            (name,)
        ).fetchone()

    if existing:
        serial_number = existing[0]
        stamps_val = existing[1] or 0
        visits_val = existing[2] if existing[2] is not None else stamps_val
        redeemed_val = existing[3] or 0
        msg_val = existing[4] or DEFAULT_MESSAGE
        final_phone = phone if phone else (existing[5] or "")
        final_bdate = birthdate if birthdate else (existing[6] or "")
        c.execute(
            """UPDATE clients
               SET name = ?, email = ?, phone = ?, birthdate = ?, updated_at = ?,
                   platform = ?, android_active = CASE WHEN ? = 1 THEN 1 ELSE android_active END
               WHERE serial = ?""",
            (name or "Cliente", email, final_phone, final_bdate, now_ts, platform, android_active, serial_number)
        )
    else:
        serial_number = f"MAISHAS-{uuid.uuid4().hex[:6].upper()}"
        stamps_val = 0
        visits_val = 0
        redeemed_val = 0
        msg_val = DEFAULT_MESSAGE
        final_phone = phone
        final_bdate = birthdate
        c.execute(
            """INSERT INTO clients (serial, name, email, phone, birthdate, stamps, total_visits, redeemed_count, message, updated_at, platform, android_active, created_at, last_action)
               VALUES (?, ?, ?, ?, ?, 0, 0, 0, ?, ?, ?, ?, ?, 'add')""",
            (serial_number, name, email, final_phone, final_bdate, msg_val, now_ts, platform, android_active, created_sonora)
        )

    conn.commit()
    conn.close()

    # Si es Android, redirigir a su Tarjeta Digital con el mismo ID unificado
    if platform == "android":
        return redirect(url_for("android_card_view", serial_number=serial_number))

    host_url = request.host_url
    pkpass_path = generate_signed_pkpass(
        serial_number, name, stamps_val, host_url, msg_val,
        phone=final_phone, birthdate=final_bdate, total_visits=visits_val, redeemed_count=redeemed_val,
        last_action="add", for_android=False
    )
    resp = send_file(
        pkpass_path,
        mimetype="application/vnd.apple.pkpass",
        as_attachment=True,
        download_name=f"MaishasCafe_{serial_number}.pkpass"
    )
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, max-age=0"
    return resp


# ---------------------------------------------------------
# ENDPOINTS DE TARJETA DIGITAL ANDROID (PWA, QR, STRIP, WEB PUSH & LIVE SYNC)
# ---------------------------------------------------------

@app.route("/card/<serial_number>")
def android_card_view(serial_number):
    serial_clean = (serial_number or "").strip().upper()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT serial, name, email, stamps, message, phone, birthdate, total_visits, redeemed_count, updated_at FROM clients WHERE UPPER(serial) = ?",
        (serial_clean,)
    )
    row = c.fetchone()
    if not row:
        conn.close()
        return "Tarjeta no encontrada", 404

    serial_id = row[0]
    c.execute("UPDATE clients SET android_active = 1, last_sync_at = ? WHERE serial = ?", (get_short_time(), serial_id))
    conn.commit()
    conn.close()

    stamps_val = row[3] or 0
    visits_val = row[7] if row[7] is not None else stamps_val
    redeemed_val = row[8] or 0
    tier = get_vip_tier(visits_val)
    reward_status = "¡CAFÉ GRATIS! 🎉" if stamps_val >= 10 else f"Faltan {10 - stamps_val} sellos"

    client_data = {
        "serial": serial_id,
        "name": row[1] or "Cliente",
        "email": row[2] or "",
        "stamps": stamps_val,
        "message": (row[4] or DEFAULT_MESSAGE).replace("\u200b", ""),
        "phone": row[5] or "",
        "birthdate": row[6] or "",
        "total_visits": visits_val,
        "redeemed_count": redeemed_val,
        "updated_at": row[9] or "",
        "tier": tier,
        "reward_status": reward_status,
    }
    return render_template_string(
        ANDROID_CARD_HTML,
        client=client_data,
        shop_loc=get_shop_location(),
        vapid_public_key=VAPID_PUBLIC_KEY,
        gw_enabled=google_wallet_enabled()
    )


@app.route("/card/<serial_number>/strip.png")
def android_card_strip(serial_number):
    serial_clean = (serial_number or "").strip().upper()
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT stamps, message FROM clients WHERE UPPER(serial) = ?", (serial_clean,)).fetchone()
    conn.close()
    stamps_val = row[0] if row else 0
    msg_val = row[1] if row else DEFAULT_MESSAGE
    img = _create_dynamic_strip_pil(stamps_val, message=msg_val).resize((750, 250), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    buf.seek(0)
    resp = send_file(buf, mimetype="image/png", max_age=0)
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp


@app.route("/card/<serial_number>/qr.png")
def android_card_qr(serial_number):
    serial_clean = (serial_number or "").strip().upper()
    qr = qrcode.QRCode(version=1, error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=8, border=1)
    qr.add_data(serial_clean)
    qr.make(fit=True)
    img = qr.make_image(fill_color="#12100E", back_color="#FFFFFF").convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return send_file(buf, mimetype="image/png", max_age=3600)


@app.route("/card/<serial_number>/download.pkpass")
def download_pkpass_direct(serial_number):
    serial_clean = (serial_number or "").strip().upper()
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT serial, name, stamps, message, phone, birthdate, total_visits, redeemed_count, last_action FROM clients WHERE UPPER(serial) = ?",
        (serial_clean,)
    ).fetchone()
    conn.close()
    if not row:
        return "No encontrado", 404

    ua = (request.headers.get("User-Agent") or "").lower()
    is_ios = ("iphone" in ua or "ipad" in ua or "ipod" in ua) and ("android" not in ua)
    pkpass_path = generate_signed_pkpass(
        row[0], row[1], row[2] or 0, request.host_url, row[3],
        phone=row[4], birthdate=row[5], total_visits=row[6], redeemed_count=row[7] or 0,
        last_action=row[8] or "add",
        for_android=not is_ios
    )
    resp = send_file(
        pkpass_path,
        mimetype="application/vnd.apple.pkpass",
        as_attachment=True,
        download_name=f"MaishasCafe_{row[0]}.pkpass"
    )
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, max-age=0"
    return resp


@app.route("/card/icon-192.png")
def card_icon_192():
    icon_path = os.path.join(PASS_TEMPLATE_DIR, "icon@2x.png")
    img = Image.open(icon_path).resize((192, 192), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return send_file(buf, mimetype="image/png", max_age=86400)


@app.route("/barista/icon-512.png")
def barista_icon_512():
    icon_path = os.path.join(PASS_TEMPLATE_DIR, "icon@2x.png")
    img = Image.open(icon_path).resize((512, 512), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return send_file(buf, mimetype="image/png", max_age=86400)


@app.route("/barista/manifest.webmanifest")
def barista_manifest():
    manifest_data = {
        "id": "/barista",
        "name": "Maishas Café — Consola Barista POS",
        "short_name": "Barista POS",
        "description": "Consola de punto de venta, escáner QR y sellos de Maishas Café",
        "start_url": "/barista",
        "scope": "/",
        "display": "standalone",
        "orientation": "any",
        "background_color": "#0D0C0B",
        "theme_color": "#0D0C0B",
        "icons": [
            {"src": "/card/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
            {"src": "/barista/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"}
        ]
    }
    return Response(json.dumps(manifest_data), mimetype="application/manifest+json")


@app.route("/card/<serial_number>/manifest.webmanifest")
def android_manifest(serial_number):
    serial_clean = (serial_number or "").strip().upper()
    manifest_data = {
        "name": "Maishas Café — Tarjeta de Fidelidad",
        "short_name": "Maishas Café",
        "start_url": f"/card/{serial_clean}",
        "display": "standalone",
        "background_color": "#0D0C0B",
        "theme_color": "#12100E",
        "icons": [
            {"src": "/card/icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "/barista/icon-512.png", "sizes": "512x512", "type": "image/png"}
        ]
    }
    return Response(json.dumps(manifest_data), mimetype="application/manifest+json")


@app.route("/sw.js")
def service_worker_js():
    sw_code = """
self.addEventListener('install', event => { self.skipWaiting(); });
self.addEventListener('activate', event => { event.waitUntil(self.clients.claim()); });
self.addEventListener('fetch', event => {});

self.addEventListener('push', event => {
    let data = {
        title: 'Maishas Café ☕️',
        body: 'Tu tarjeta de fidelidad se ha actualizado.',
        url: '/',
        pkpass_url: '',
        image: '',
        serial: ''
    };
    if (event.data) {
        try { data = Object.assign(data, event.data.json()); }
        catch (e) { data.body = event.data.text(); }
    }
    const options = {
        body: data.body,
        icon: '/card/icon-192.png',
        badge: '/card/icon-192.png',
        vibrate: [200, 100, 200],
        tag: data.tag || ('maishas-push-' + Date.now()),
        renotify: true,
        requireInteraction: true,
        data: {
            url: data.url || '/',
            pkpass_url: data.pkpass_url || '',
            serial: data.serial || ''
        },
        actions: [
            { action: 'open_card', title: '📲 Ver Tarjeta en Vivo' },
            { action: 'update_pkpass', title: '🔄 Actualizar Wallet' }
        ]
    };
    if (data.image) {
        options.image = data.image;
    }
    event.waitUntil(
        Promise.all([
            self.registration.showNotification(data.title, options),
            self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then(windowClients => {
                for (let client of windowClients) {
                    client.postMessage({ type: 'MAISHAS_PUSH', data: data });
                }
            })
        ])
    );
});

self.addEventListener('notificationclick', event => {
    event.notification.close();
    const notifData = event.notification.data || {};
    let targetUrl = notifData.url || '/';
    if (event.action === 'update_pkpass' && notifData.serial) {
        targetUrl = '/card/' + notifData.serial + '?auto_pkpass=1&t=' + Date.now();
    }
    event.waitUntil(
        clients.matchAll({ type: 'window', includeUncontrolled: true }).then(windowClients => {
            if (event.action !== 'update_pkpass') {
                for (let client of windowClients) {
                    if (notifData.serial && client.url.includes('/card/' + notifData.serial) && 'focus' in client) {
                        client.postMessage({ type: 'MAISHAS_PUSH', data: notifData });
                        return client.focus();
                    }
                }
            }
            if (clients.openWindow) return clients.openWindow(targetUrl);
        })
    );
});
"""
    resp = Response(sw_code.strip(), mimetype="application/javascript")
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp


@app.route("/api/android/heartbeat/<serial_number>", methods=["POST"])
def android_heartbeat(serial_number):
    serial_clean = (serial_number or "").strip().upper()
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE clients SET android_active = 1, last_sync_at = ? WHERE UPPER(serial) = ?", (get_short_time(), serial_clean))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/android/subscribe/<serial_number>", methods=["POST"])
def android_subscribe(serial_number):
    serial_clean = (serial_number or "").strip().upper()
    payload = request.get_json(force=True, silent=True) or {}
    sub = payload.get("subscription") or {}
    endpoint = sub.get("endpoint")
    if not endpoint:
        return jsonify({"ok": False, "error": "Missing endpoint"}), 400

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "INSERT OR REPLACE INTO android_subscriptions (serial, endpoint, sub_json, created_at) VALUES (?, ?, ?, ?)",
        (serial_clean, endpoint, json.dumps(sub), get_utc_timestamp())
    )
    c.execute(
        "UPDATE clients SET android_active = 1, last_sync_at = ?, last_push_status = ? WHERE UPPER(serial) = ?",
        (get_short_time(), f"✅ Push Android activado ({get_short_time()})", serial_clean)
    )
    c.execute(
        "INSERT INTO wallet_logs (created_at, log_text) VALUES (?, ?)",
        (get_utc_timestamp(), f"🔔 Suscripción Web Push Android registrada para {serial_clean}")
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/android/state/<serial_number>")
def android_card_state(serial_number):
    serial_clean = (serial_number or "").strip().upper()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT serial, name, stamps, message, total_visits, redeemed_count, updated_at FROM clients WHERE UPPER(serial) = ?",
        (serial_clean,)
    )
    row = c.fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "Not found"}), 404

    # Registrar que el teléfono Android está sincronizado en vivo
    c.execute("UPDATE clients SET android_active = 1, last_sync_at = ? WHERE serial = ?", (get_short_time(), row[0]))
    conn.commit()
    conn.close()

    stamps_val = row[2] or 0
    visits_val = row[4] if row[4] is not None else stamps_val
    redeemed_val = row[5] or 0
    tier = get_vip_tier(visits_val)
    reward_status = "¡CAFÉ GRATIS! 🎉" if stamps_val >= 10 else f"Faltan {10 - stamps_val} sellos"
    clean_msg = (row[3] or DEFAULT_MESSAGE).replace("\u200b", "")

    return jsonify({
        "serial": row[0],
        "name": row[1],
        "stamps": stamps_val,
        "message": clean_msg,
        "tier_name": tier["name"],
        "reward_status": reward_status,
        "history_text": f"Nivel: {tier['name']} | Visitas históricas: {visits_val} | Cafés gratis canjeados: {redeemed_val} ({tier['next_goal']})",
        "updated_at": row[6] or ""
    })


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
        action_label = f"Ajuste aplicado a {client_name} ({serial_id}): saldo actual {new_stamps}/10"
        notif_body = f"☕️ Tu tarjeta Maishas Café está al día. Sellos acumulados: {new_stamps} / 10"
    elif action == "reset":
        new_stamps = 0
        new_visits = current_visits
        new_redeemed = current_redeemed + 1
        action_label = f"🎁 Café Gratis canjeado para {client_name} ({serial_id}): reiniciado a 0/10 (Total canjes: {new_redeemed}){bday_alert}"
        notif_body = "🎁 ¡Disfruta tu Café Gratis! Tu tarjeta se ha reiniciado a 0 / 10."
    else:
        action = "add"
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
        if new_stamps >= 10:
            notif_body = "🎉 ¡Felicidades! Completaste 10/10 sellos. ¡Tienes 1 Café Gratis disponible en Maishas Café!"
        else:
            notif_body = f"☕️ ¡Has ganado 1 nuevo sello en Maishas Café! Total: {new_stamps} / 10"

    now_ts = get_utc_timestamp()
    c.execute(
        "UPDATE clients SET stamps = ?, total_visits = ?, redeemed_count = ?, updated_at = ?, last_action = ? WHERE serial = ?",
        (new_stamps, new_visits, new_redeemed, now_ts, action, serial_id)
    )
    conn.commit()
    conn.close()

    push_summary = send_apns_push_for_serial(serial_id, notification_body=notif_body)
    return serial_id, f"{action_label} · {push_summary}"


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
        return redirect(url_for("barista", ok="⚠️ Latitud y Longitud deben ser números válidos (ej. 27.0706, -109.4437)"))

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
        res = send_apns_push_for_serial(s, notification_body=relevant_text)
        if "✅" in res:
            pushed += 1

    return redirect(url_for(
        "barista",
        ok=f"📍 Ubicación GPS guardada ({lat:.5f}, {lng:.5f}) y sincronizada con {pushed} teléfono(s)"
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
    c.execute("DELETE FROM android_subscriptions WHERE serial = ?", (serial,))
    c.execute("DELETE FROM clients WHERE serial = ?", (serial,))
    conn.commit()
    conn.close()
    name = row[0] if row else serial
    return redirect(url_for("barista", ok=f"🗑️ Cliente {name} ({serial}) eliminado correctamente"))


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
        last_res = send_apns_push_for_serial(serial_id, notification_body=f"📣 {raw_message}")
        if "✅" in last_res:
            pushed_ok += 1

    if target == "all":
        msg_status = f"📣 Aviso enviado a {pushed_ok} teléfono(s) activos ({get_short_time()})"
        return redirect(url_for("barista", ok=msg_status))
    else:
        target_name = rows[0][1] if rows else target
        return redirect(url_for("barista", ok=f"📣 Aviso enviado a {target_name} · {last_res}", updated=target))


# ---------------------------------------------------------
# APPLE WALLET WEB SERVICE PROTOCOL & ANDROID WEB PUSH DISPATCHER
# ---------------------------------------------------------

def is_authorized():
    auth = request.headers.get("Authorization", "").strip()
    return (AUTH_TOKEN in auth) or auth in (
        f"ApplePass {AUTH_TOKEN}",
        f"AndroidPass {AUTH_TOKEN}",
        f"AttidoPass {AUTH_TOKEN}",
    ) or auth.endswith(f" {AUTH_TOKEN}")


@app.route("/v1/devices/<device_id>/registrations/<pass_type_id>/<serial_number>", methods=["POST"])
def register_device(device_id, pass_type_id, serial_number):
    if not is_authorized():
        return "", 401
    payload = request.get_json(force=True, silent=True) or {}
    push_token = (payload.get("pushToken") or payload.get("push_token") or "").strip()
    push_service_url = (payload.get("pushServiceUrl") or payload.get("push_service_url") or "").strip()
    if not push_token:
        return "", 400

    serial_clean = (serial_number or "").strip().upper()
    auth_hdr = request.headers.get("Authorization", "")
    ua_hdr = (request.headers.get("User-Agent") or "").lower()
    is_android_wallet_app = (
        auth_hdr.startswith("AndroidPass")
        or auth_hdr.startswith("AttidoPass")
        or "android" in ua_hdr
        or "walletpasses" in ua_hdr
        or bool(push_service_url)
    )

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT 1 FROM registrations WHERE device_id = ? AND UPPER(serial) = ?", (device_id, serial_clean))
    exists = c.fetchone()
    c.execute(
        "INSERT OR REPLACE INTO registrations (device_id, push_token, serial, push_service_url) VALUES (?, ?, ?, ?)",
        (device_id, push_token, serial_clean, push_service_url)
    )
    if is_android_wallet_app:
        c.execute(
            "UPDATE clients SET push_token = ?, android_active = 1, last_sync_at = ? WHERE UPPER(serial) = ?",
            (push_token, get_short_time(), serial_clean)
        )
    else:
        c.execute(
            "UPDATE clients SET push_token = ?, last_sync_at = ? WHERE UPPER(serial) = ?",
            (push_token, get_short_time(), serial_clean)
        )
    c.execute(
        "INSERT INTO wallet_logs (created_at, log_text) VALUES (?, ?)",
        (get_utc_timestamp(), f"📲 Registro PassKit ({'Android' if is_android_wallet_app else 'iPhone'}): serial={serial_clean} device={device_id[:10]}")
    )
    conn.commit()
    conn.close()

    print(f"📲 Wallet registrado ({'Android' if is_android_wallet_app else 'iPhone'}): device={device_id} serial={serial_clean}")
    return "", (200 if exists else 201)


@app.route("/v1/devices/<device_id>/registrations/<pass_type_id>/<serial_number>", methods=["DELETE"])
def unregister_device(device_id, pass_type_id, serial_number):
    if not is_authorized():
        return "", 401
    serial_clean = (serial_number or "").strip().upper()
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM registrations WHERE device_id = ? AND UPPER(serial) = ?", (device_id, serial_clean))
    conn.execute(
        "UPDATE clients SET push_token = NULL WHERE UPPER(serial) = ? AND UPPER(serial) NOT IN (SELECT UPPER(serial) FROM registrations)",
        (serial_clean,)
    )
    conn.commit()
    conn.close()
    return "", 200


@app.route("/v1/devices/<device_id>/registrations/<pass_type_id>", methods=["GET"])
def get_updated_passes(device_id, pass_type_id):
    since = request.args.get("passesUpdatedSince")
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    query = """SELECT c.serial, c.updated_at FROM clients c
               JOIN registrations r ON UPPER(r.serial) = UPPER(c.serial)
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


@app.route("/v1/passes/<pass_type_id>/<serial_number>", methods=["GET"])
def get_latest_pass(pass_type_id, serial_number):
    if not is_authorized():
        return "", 401
    serial_clean = (serial_number or "").strip().upper()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT serial, name, stamps, message, phone, birthdate, total_visits, redeemed_count, updated_at, last_action FROM clients WHERE UPPER(serial) = ?",
        (serial_clean,)
    )
    client = c.fetchone()
    if not client:
        conn.close()
        return "", 404

    serial_id, name, stamps, message, phone, birthdate, total_visits, redeemed_count, updated_at_str, last_action = client

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

    auth_hdr = request.headers.get("Authorization", "")
    ua_hdr = (request.headers.get("User-Agent") or "").lower()
    is_android_wallet = auth_hdr.startswith("AndroidPass") or auth_hdr.startswith("AttidoPass") or "android" in ua_hdr or "walletpasses" in ua_hdr

    c.execute("UPDATE clients SET last_sync_at = ? WHERE serial = ?", (get_short_time(), serial_id))
    c.execute(
        "INSERT INTO wallet_logs (created_at, log_text) VALUES (?, ?)",
        (get_utc_timestamp(), f"🔄 Pase descargado/actualizado ({'Android' if is_android_wallet else 'iPhone'}): {serial_id} ({stamps}/10)")
    )
    conn.commit()
    conn.close()

    pkpass_path = generate_signed_pkpass(
        serial_id, name, stamps or 0, request.host_url, message,
        phone=phone, birthdate=birthdate, total_visits=total_visits, redeemed_count=redeemed_count or 0,
        last_action=last_action or "add",
        for_android=is_android_wallet
    )
    response = send_file(pkpass_path, mimetype="application/vnd.apple.pkpass", conditional=False, max_age=0)
    response.headers["Last-Modified"] = formatdate(timeval=float(upd_epoch_sec), localtime=False, usegmt=True)
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response


@app.route("/v1/log", methods=["POST"])
def wallet_log():
    logs = (request.get_json(force=True, silent=True) or {}).get("logs", [])
    if logs:
        conn = sqlite3.connect(DB_PATH)
        for line in logs:
            print(f"📝 Wallet log: {line}")
            conn.execute("INSERT INTO wallet_logs (created_at, log_text) VALUES (?, ?)", (get_utc_timestamp(), str(line)))
        conn.commit()
        conn.close()
    return "", 200


def _get_vapid_signer():
    """Devuelve un objeto Vapid válido. pywebpush interpreta un str como ruta/base64, NO como PEM,
    por eso hay que pasar una instancia de Vapid cargada desde el PEM."""
    return Vapid02.from_pem(VAPID_PRIVATE_PEM.encode("utf-8"))


def send_android_webpush(sub_dict, serial, body_text, stamps=None):
    """Envía notificación Web Push RFC 8291 (VAPID) a un teléfono Android con imagen en vivo del pase."""
    try:
        ts_val = int(datetime.now(timezone.utc).timestamp())
        title_str = f"Maishas Café ☕️ ({stamps}/10 sellos)" if stamps is not None else "Maishas Café ☕️"
        payload = json.dumps({
            "title": title_str,
            "body": body_text,
            "url": f"/card/{serial}?from_push=1&t={ts_val}",
            "pkpass_url": f"/card/{serial}/download.pkpass?v={ts_val}",
            "image": f"/card/{serial}/strip.png?v={ts_val}",
            "tag": f"maishas-{serial}-{ts_val}",
            "serial": serial
        })
        webpush(
            subscription_info=sub_dict,
            data=payload,
            vapid_private_key=_get_vapid_signer(),
            vapid_claims={"sub": "mailto:contacto@maishascafe.com"},
            ttl=86400,
            headers={"Urgency": "high"},
            timeout=10
        )
        return True, 200, "OK"
    except WebPushException as ex:
        code = ex.response.status_code if ex.response is not None else 500
        detail = f"{ex}"
        if ex.response is not None:
            try:
                detail += f" | {ex.response.text[:200]}"
            except Exception:
                pass
        _log_wallet_event(f"⚠️ Web Push Android falló ({serial}): HTTP {code} {detail[:300]}")
        return False, code, detail
    except Exception as e:
        _log_wallet_event(f"⚠️ Web Push Android error interno ({serial}): {str(e)[:300]}")
        return False, 500, str(e)


def _log_wallet_event(text):
    """Guarda un evento de diagnóstico en wallet_logs (visible en /api/diagnostics)."""
    try:
        print(text)
        conn = sqlite3.connect(DB_PATH)
        conn.execute("INSERT INTO wallet_logs (created_at, log_text) VALUES (?, ?)", (get_utc_timestamp(), text))
        conn.commit()
        conn.close()
    except Exception:
        pass


def send_apns_push_for_serial(serial, notification_body="Tu tarjeta Maishas Café se ha actualizado ☕️"):
    """Despacha notificaciones Push a iPhone y Android y devuelve un estado claro en hora local de Navojoa, Sonora."""
    serial_clean = (serial or "").strip().upper()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    reg_rows = c.execute(
        "SELECT push_token, COALESCE(push_service_url, '') FROM registrations WHERE UPPER(serial) = ? AND push_token IS NOT NULL",
        (serial_clean,)
    ).fetchall()
    client_row = c.execute(
        "SELECT push_token, android_active, stamps FROM clients WHERE UPPER(serial) = ?",
        (serial_clean,)
    ).fetchone()

    token_map = {}
    for tok, srv_url in reg_rows:
        if tok:
            token_map[tok] = srv_url
    if client_row and client_row[0] and client_row[0] not in token_map:
        token_map[client_row[0]] = ""

    android_active = bool(client_row[1]) if client_row else False
    current_stamps = (client_row[2] or 0) if client_row else 0

    android_subs = c.execute(
        "SELECT endpoint, sub_json FROM android_subscriptions WHERE UPPER(serial) = ?",
        (serial_clean,)
    ).fetchall()

    any_success = False
    any_error = False

    # 1. Enviar a iPhones registrados (Apple APNs HTTP/2) o Apps Android .pkpass (WalletPasses / Attido)
    for token, srv_url in token_map.items():
        ok, status_code, detail = send_apns_push(token, push_service_url=srv_url)
        if ok:
            any_success = True
        else:
            any_error = True
            if status_code == 410 or "Unregistered" in detail or "BadDeviceToken" in detail:
                c.execute("DELETE FROM registrations WHERE push_token = ? AND UPPER(serial) = ?", (token, serial_clean))

    # 2. Enviar a suscripciones Web Push de Android (Chrome / PWA)
    conn.commit()  # liberar el lock de SQLite antes de que send_android_webpush escriba logs
    android_ok = 0
    android_fail = 0
    stale_endpoints = []
    for endpoint, sub_json_str in android_subs:
        try:
            sub_dict = json.loads(sub_json_str)
            ok_and, code_and, _ = send_android_webpush(sub_dict, serial_clean, notification_body, stamps=current_stamps)
            if ok_and:
                any_success = True
                android_ok += 1
            else:
                any_error = True
                android_fail += 1
                if code_and in (404, 410):
                    stale_endpoints.append(endpoint)
        except Exception:
            any_error = True
            android_fail += 1
    for endpoint in stale_endpoints:
        c.execute("DELETE FROM android_subscriptions WHERE endpoint = ?", (endpoint,))

    # 3. Google Wallet (Android): actualizar el pase nativo y enviar el aviso. Solo clientes Android; no afecta a Apple.
    if google_wallet_enabled() and (android_active or android_subs):
        conn.commit()
        try:
            if google_wallet_push_update(serial_clean, _public_base_url(), message=notification_body):
                any_success = True
        except Exception as gw_err:
            _log_wallet_event(f"⚠️ Google Wallet: error al actualizar {serial_clean}: {str(gw_err)[:200]}")

    sonora_now = get_short_time()
    if any_success:
        status_msg = f"✅ Notificación enviada ({sonora_now})"
    elif android_fail and not token_map:
        status_msg = f"⚠️ Android no recibió el Push ({sonora_now}) · Pídele abrir su tarjeta y tocar «Activar Notificaciones»"
    elif android_active and not android_subs:
        status_msg = f"🔄 Tarjeta web al día ({sonora_now}) · Falta activar Push en su Android"
    elif any_error:
        status_msg = f"⚠️ No se pudo notificar ({sonora_now})"
    else:
        status_msg = "⏳ Aún no instala su tarjeta"

    c.execute("UPDATE clients SET last_push_status = ? WHERE UPPER(serial) = ?", (status_msg, serial_clean))
    conn.commit()
    conn.close()
    return status_msg


def send_apns_push(push_token, push_service_url=None):
    try:
        # Detectar si el token pertenece a una app Android de .pkpass (ej. WalletPasses FCM token) vs iOS APNs hex token
        is_ios_hex = len(push_token) in (64, 128) and all(c in "0123456789abcdefABCDEF" for c in push_token)
        if not is_ios_hex:
            target_url = push_service_url if (push_service_url and push_service_url.startswith("http")) else "https://walletpasses.appspot.com/api/v1/push"
            with httpx.Client(timeout=8.0) as client:
                res = client.post(
                    target_url,
                    json={"passTypeIdentifier": PASS_TYPE_ID, "pushTokens": [push_token]}
                )
                if res.status_code in (200, 201, 202, 204):
                    return True, 200, "OK"
                return False, res.status_code, res.text

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


# ---------------------------------------------------------
# GOOGLE WALLET API (ANDROID) — pase nativo que se actualiza solo, equivalente a Apple Wallet
# Se activa únicamente si existen GOOGLE_WALLET_ISSUER_ID y GOOGLE_WALLET_SERVICE_ACCOUNT_JSON.
# ---------------------------------------------------------
GOOGLE_WALLET_ISSUER_ID = (os.environ.get("GOOGLE_WALLET_ISSUER_ID") or "").strip()
GOOGLE_WALLET_CLASS_SUFFIX = "maishas_loyalty_v1"
GW_API_BASE = "https://walletobjects.googleapis.com/walletobjects/v1"
GW_SCOPE = "https://www.googleapis.com/auth/wallet_object.issuer"
PUBLIC_BASE_URL = (os.environ.get("PUBLIC_BASE_URL") or "https://web-production-98096.up.railway.app").rstrip("/")
_gw_token_cache = {"token": None, "exp": 0}
_gw_class_ready = {"ok": False}


def _load_google_sa():
    """Lee la cuenta de servicio de Google (JSON directo o en base64) desde la variable de entorno."""
    raw = (os.environ.get("GOOGLE_WALLET_SERVICE_ACCOUNT_JSON") or "").strip()
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except Exception:
        try:
            data = json.loads(base64.b64decode(raw).decode("utf-8"))
        except Exception:
            return None
    if not (data.get("client_email") and data.get("private_key")):
        return None
    data["private_key"] = data["private_key"].replace("\\n", "\n")
    return data


def google_wallet_enabled():
    return bool(GOOGLE_WALLET_ISSUER_ID and _load_google_sa())


def _public_base_url():
    try:
        if request:
            host = request.host_url.rstrip("/")
            if "localhost" not in host and "127.0.0.1" not in host:
                return host.replace("http://", "https://", 1)
    except Exception:
        pass
    return PUBLIC_BASE_URL


def _gw_b64url(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _gw_sign_jwt(claims, sa):
    """JWT RS256 firmado con la llave privada de la cuenta de servicio."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    header = {"alg": "RS256", "typ": "JWT"}
    signing_input = (
        _gw_b64url(json.dumps(header, separators=(",", ":")).encode())
        + "." + _gw_b64url(json.dumps(claims, separators=(",", ":")).encode())
    )
    key = serialization.load_pem_private_key(sa["private_key"].encode("utf-8"), password=None)
    sig = key.sign(signing_input.encode("ascii"), padding.PKCS1v15(), hashes.SHA256())
    return signing_input + "." + _gw_b64url(sig)


def _gw_http(method, url, headers=None, json_body=None, data=None):
    with httpx.Client(timeout=15.0) as client:
        res = client.request(method, url, headers=headers, json=json_body, data=data)
    try:
        body = res.json()
    except Exception:
        body = {"raw": res.text[:300]}
    return res.status_code, body


def _gw_access_token():
    now = int(datetime.now(timezone.utc).timestamp())
    if _gw_token_cache["token"] and _gw_token_cache["exp"] - 60 > now:
        return _gw_token_cache["token"]
    sa = _load_google_sa()
    if not sa:
        return None
    assertion = _gw_sign_jwt({
        "iss": sa["client_email"], "scope": GW_SCOPE,
        "aud": sa.get("token_uri") or "https://oauth2.googleapis.com/token",
        "iat": now, "exp": now + 3600
    }, sa)
    status, body = _gw_http(
        "POST", sa.get("token_uri") or "https://oauth2.googleapis.com/token",
        data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion}
    )
    if status != 200 or "access_token" not in body:
        _log_wallet_event(f"⚠️ Google Wallet: no se pudo obtener token ({status}): {str(body)[:200]}")
        return None
    _gw_token_cache["token"] = body["access_token"]
    _gw_token_cache["exp"] = now + int(body.get("expires_in", 3600))
    return _gw_token_cache["token"]


def _gw_api(method, path, json_body=None):
    token = _gw_access_token()
    if not token:
        return 0, {"error": "sin token de Google (revisa credenciales)"}
    return _gw_http(method, f"{GW_API_BASE}/{path}", headers={"Authorization": f"Bearer {token}"}, json_body=json_body)


def _gw_class_id():
    return f"{GOOGLE_WALLET_ISSUER_ID}.{GOOGLE_WALLET_CLASS_SUFFIX}"


def _gw_object_id(serial):
    return f"{GOOGLE_WALLET_ISSUER_ID}.{(serial or '').strip().upper()}"


def _gw_text(value):
    return {"defaultValue": {"language": "es-MX", "value": value}}


def build_google_loyalty_class(base_url):
    loc = get_shop_location()
    return {
        "id": _gw_class_id(),
        "issuerName": "Maishas Café",
        "programName": "Maishas Café · Sellos",
        "programLogo": {
            "sourceUri": {"uri": f"{base_url}/card/icon-192.png"},
            "contentDescription": _gw_text("Maishas Café")
        },
        "hexBackgroundColor": "#12100E",
        "countryCode": "MX",
        "reviewStatus": "UNDER_REVIEW",
        "multipleDevicesAndHoldersAllowedStatus": "ONE_USER_ALL_DEVICES",
        "homepageUri": {"uri": base_url, "description": "Maishas Café"},
        "locations": [{"latitude": loc["lat"], "longitude": loc["lng"]}],
    }


def build_google_loyalty_object(serial, base_url):
    """Construye el pase de lealtad de Google con los datos actuales del cliente (sellos, nivel, aviso, strip)."""
    serial_clean = (serial or "").strip().upper()
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT serial, name, stamps, message, total_visits, updated_at FROM clients WHERE UPPER(serial) = ?",
        (serial_clean,)
    ).fetchone()
    conn.close()
    if not row:
        return None
    serial_id, name, stamps, message, visits, updated_at = row
    stamps = max(0, min(10, stamps or 0))
    tier = get_vip_tier(visits if visits is not None else stamps)
    reward = "¡CAFÉ GRATIS DISPONIBLE! 🎉" if stamps >= 10 else f"Faltan {10 - stamps} sellos para tu café gratis"
    clean_msg = (message or DEFAULT_MESSAGE).replace("\u200b", "")
    loc = get_shop_location()
    version = urllib_quote(str(updated_at or ""))
    return {
        "id": _gw_object_id(serial_id),
        "classId": _gw_class_id(),
        "state": "ACTIVE",
        "accountId": serial_id,
        "accountName": name or "Cliente",
        "loyaltyPoints": {"label": "SELLOS", "balance": {"string": f"{stamps} / 10"}},
        "barcode": {"type": "QR_CODE", "value": serial_id, "alternateText": serial_id},
        "heroImage": {
            "sourceUri": {"uri": f"{base_url}/card/{serial_id}/strip.png?v={version}"},
            "contentDescription": _gw_text(f"{stamps} de 10 sellos")
        },
        "textModulesData": [
            {"id": "tier", "header": "NIVEL VIP", "body": tier["name"]},
            {"id": "reward", "header": "PREMIO", "body": reward},
            {"id": "promo", "header": "AVISO MAISHAS", "body": clean_msg},
        ],
        "linksModuleData": {"uris": [
            {"id": "live", "uri": f"{base_url}/card/{serial_id}", "description": "Mi tarjeta en vivo"}
        ]},
        "locations": [{"latitude": loc["lat"], "longitude": loc["lng"]}],
    }


def urllib_quote(text):
    from urllib.parse import quote
    return quote(text, safe="")


def ensure_google_class(base_url, force_update=False):
    """Crea o actualiza la clase de lealtad (una sola para todos los clientes)."""
    body = build_google_loyalty_class(base_url)
    status, resp = _gw_api("GET", f"loyaltyClass/{_gw_class_id()}")
    if status == 404:
        status, resp = _gw_api("POST", "loyaltyClass", body)
    elif status == 200 and force_update:
        patch = {k: v for k, v in body.items() if k != "id"}
        status, resp = _gw_api("PATCH", f"loyaltyClass/{_gw_class_id()}", patch)
    ok = status in (200, 201)
    _gw_class_ready["ok"] = ok
    if not ok:
        _log_wallet_event(f"⚠️ Google Wallet: error con la clase ({status}): {str(resp)[:250]}")
    return ok, status, resp


def ensure_google_object(serial, base_url):
    """Crea el pase del cliente en Google (si no existe) o lo actualiza con sus datos actuales."""
    obj = build_google_loyalty_object(serial, base_url)
    if not obj:
        return False, 404, {"error": "cliente no encontrado"}
    if not _gw_class_ready["ok"]:
        ok, st, resp = ensure_google_class(base_url)
        if not ok:
            return False, st, resp
    status, resp = _gw_api("GET", f"loyaltyObject/{obj['id']}")
    if status == 404:
        status, resp = _gw_api("POST", "loyaltyObject", obj)
    elif status == 200:
        patch = {k: v for k, v in obj.items() if k not in ("id", "classId")}
        status, resp = _gw_api("PATCH", f"loyaltyObject/{obj['id']}", patch)
    ok = status in (200, 201)
    if not ok:
        _log_wallet_event(f"⚠️ Google Wallet: error con el pase de {serial} ({status}): {str(resp)[:250]}")
    return ok, status, resp


def google_wallet_save_url(serial, base_url):
    """Link 'Agregar a Google Wallet' firmado (JWT corto: solo referencia el id del pase ya creado)."""
    ok, status, resp = ensure_google_object(serial, base_url)
    if not ok:
        return None, f"Google respondió {status}: {str(resp)[:200]}"
    sa = _load_google_sa()
    now = int(datetime.now(timezone.utc).timestamp())
    claims = {
        "iss": sa["client_email"], "aud": "google", "typ": "savetowallet", "iat": now,
        "origins": [base_url],
        "payload": {"loyaltyObjects": [{"id": _gw_object_id(serial)}]},
    }
    return f"https://pay.google.com/gp/v/save/{_gw_sign_jwt(claims, sa)}", None


def google_wallet_push_update(serial, base_url, message=None, notify=True):
    """Actualiza el pase del cliente en Google Wallet y, si hay mensaje, envía el aviso al teléfono."""
    if not google_wallet_enabled():
        return False
    serial_clean = (serial or "").strip().upper()
    obj_id = _gw_object_id(serial_clean)
    status, _ = _gw_api("GET", f"loyaltyObject/{obj_id}")
    if status != 200:
        return False  # el cliente aún no ha agregado su pase a Google Wallet
    ok, st, _ = ensure_google_object(serial_clean, base_url)
    if not ok:
        return False
    if message and notify:
        st2, resp2 = _gw_api("POST", f"loyaltyObject/{obj_id}/addMessage", {
            "message": {
                "header": "Maishas Café ☕️",
                "body": message[:180],
                "messageType": "TEXT_AND_NOTIFY",
            }
        })
        if st2 not in (200, 201):
            _log_wallet_event(f"⚠️ Google Wallet: aviso no enviado a {serial_clean} ({st2}): {str(resp2)[:200]}")
    return True


@app.route("/card/<serial_number>/google-wallet")
def google_wallet_save(serial_number):
    """Redirige al botón oficial 'Agregar a Google Wallet' del cliente."""
    if not google_wallet_enabled():
        return "Google Wallet aún no está configurado en el servidor.", 503
    serial_clean = (serial_number or "").strip().upper()
    url, err = google_wallet_save_url(serial_clean, _public_base_url())
    if not url:
        return f"No se pudo generar el pase de Google Wallet. {err}", 502
    _log_wallet_event(f"🟢 Google Wallet: enlace de guardado generado para {serial_clean}")
    return redirect(url)


@app.route("/api/google-wallet/setup")
@requires_auth
def google_wallet_setup_check():
    """Verifica las credenciales de Google Wallet y crea/actualiza la clase de lealtad."""
    sa = _load_google_sa()
    info = {
        "issuer_id_configurado": bool(GOOGLE_WALLET_ISSUER_ID),
        "cuenta_servicio_valida": bool(sa),
        "cuenta_servicio_email": sa["client_email"] if sa else None,
        "habilitado": google_wallet_enabled(),
    }
    if info["habilitado"]:
        ok, status, resp = ensure_google_class(_public_base_url())
        info["clase_ok"] = ok
        info["clase_http"] = status
        info["clase_detalle"] = resp if not ok else {"id": _gw_class_id()}
    return jsonify(info)


@app.route("/api/test-android-push/<serial_number>")
@requires_auth
def test_android_push(serial_number):
    """Envía un Web Push de prueba y devuelve el resultado real por cada suscripción (para diagnóstico)."""
    serial_clean = (serial_number or "").strip().upper()
    conn = sqlite3.connect(DB_PATH)
    subs = conn.execute(
        "SELECT endpoint, sub_json, created_at FROM android_subscriptions WHERE UPPER(serial) = ?",
        (serial_clean,)
    ).fetchall()
    row = conn.execute("SELECT stamps FROM clients WHERE UPPER(serial) = ?", (serial_clean,)).fetchone()
    all_subs = conn.execute("SELECT serial, endpoint, created_at FROM android_subscriptions").fetchall()
    conn.close()
    results = []
    for endpoint, sub_json_str, created_at in subs:
        ok, code, detail = send_android_webpush(json.loads(sub_json_str), serial_clean, "🔔 Prueba de notificación Maishas Café", stamps=(row[0] if row else 0))
        results.append({"endpoint_host": endpoint.split("/")[2], "created_at": created_at, "ok": ok, "http_status": code, "detail": detail[:300]})
    return jsonify({
        "serial": serial_clean,
        "client_exists": bool(row),
        "subscriptions_for_serial": len(subs),
        "results": results,
        "all_subscriptions_in_db": [{"serial": s, "host": e.split("/")[2], "created_at": c} for s, e, c in all_subs],
    })


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
        "android_wallet_support": {
            "vapid_configured": bool(VAPID_PUBLIC_KEY),
            "qr_generator": True,
            "pwa_service_worker": True,
        },
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
    report["database"]["total_ios_registrations"] = c.execute("SELECT COUNT(*) FROM registrations").fetchone()[0]
    report["database"]["total_android_subscriptions"] = c.execute("SELECT COUNT(*) FROM android_subscriptions").fetchone()[0]
    report["recent_wallet_logs"] = [
        {"time": r[0], "log": r[1]}
        for r in c.execute("SELECT created_at, log_text FROM wallet_logs ORDER BY id DESC LIMIT 20").fetchall()
    ]
    conn.close()
    return jsonify(report)


if __name__ == "__main__":
    print("🚀 Servidor con soporte iOS Apple Wallet + Android Wallet Digital iniciado en http://localhost:5001")
    port = int(os.environ.get('PORT', 5001))
    app.run(host="0.0.0.0", port=port, debug=False)
