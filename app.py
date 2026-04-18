# app.py — Render + Supabase deployment version
import os
import io
import logging
import qrcode
import csv
import zipfile
import textwrap
import tempfile
from io import BytesIO, StringIO
from datetime import datetime
from functools import wraps
from urllib.parse import quote as url_encode
from whatsapp import send_guest_card
from sms_africastalking import send_sms as at_send_sms, is_configured as at_configured
import time


from flask import (
    Flask, render_template, request, redirect, url_for, flash,
    session, jsonify, send_file, make_response, current_app
)
from werkzeug.utils import secure_filename
from dotenv import dotenv_values, load_dotenv
from PIL import Image, ImageDraw, ImageFont
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.formatting.rule import CellIsRule
from sqlalchemy.sql import func
from sqlalchemy.exc import IntegrityError

# Supabase client
from supabase import create_client, Client

from models import Guest, init_db, get_db_session

# ---------------------------------------------------------------------------
# Environment Loading
# ---------------------------------------------------------------------------
flask_env = os.getenv('FLASK_ENV', 'production')
WHATSAPP_PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID")
WHATSAPP_ACCESS_TOKEN = os.getenv("WHATSAPP_ACCESS_TOKEN")
WHATSAPP_VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN", "wedding_webhook_secret")


if flask_env == 'development':
    current_env_file = '.env.development'
else:
    current_env_file = '.env'

if os.path.exists(current_env_file):
    config = dotenv_values(current_env_file)
    for key, value in config.items():
        if value is not None:
            os.environ[key] = value
else:
    logging.warning(f"Env file {current_env_file} not found; using existing env vars.")

# ---------------------------------------------------------------------------
# Database Configuration
# ---------------------------------------------------------------------------
DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    DB_FILE = os.getenv("DB_FILE", "guests.db")
    DATABASE_URL = f"sqlite:///./{DB_FILE}"

if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

# ---------------------------------------------------------------------------
# Supabase Storage Configuration
# ---------------------------------------------------------------------------
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY")
QR_BUCKET = os.getenv("SUPABASE_QR_BUCKET", "qr-codes")
CARDS_BUCKET = os.getenv("SUPABASE_CARDS_BUCKET", "guest-cards")

supabase: Client = None
if SUPABASE_URL and SUPABASE_KEY:
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    logging.info("Supabase client initialized.")
else:
    logging.warning("SUPABASE_URL or SUPABASE_SERVICE_KEY not set — storage features disabled.")

# ---------------------------------------------------------------------------
# Flask App
# ---------------------------------------------------------------------------
app = Flask(__name__)

app.config['SECRET_KEY'] = os.getenv('SECRET_KEY')
if not app.config['SECRET_KEY']:
    raise ValueError("SECRET_KEY environment variable is not set.")

app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL

UPLOAD_FOLDER = "uploads"
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logging.info(f"Using database: {DATABASE_URL}")

ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "WedSy#01")

with app.app_context():
    init_db(app)

# ---------------------------------------------------------------------------
# Card rendering constants  (all pixel values for 1240 × 1748 px template)
# ---------------------------------------------------------------------------

# Font: prefer Montserrat Bold if placed in static/fonts/, else fall back to
# Poppins Bold which ships on the server (same geometric sans-serif family).
_MONTSERRAT_PATH = os.path.join("static", "fonts", "Montserrat-Bold.ttf")
_POPPINS_PATH    = "/usr/share/fonts/truetype/google-fonts/Poppins-Bold.ttf"
_ROBOTO_PATH     = os.path.join("static", "fonts", "Roboto-Bold.ttf")

def _bold_font_path() -> str:
    """Return the best available bold font path."""
    for p in (_MONTSERRAT_PATH, _ROBOTO_PATH, _POPPINS_PATH):
        if os.path.exists(p):
            return p
    raise FileNotFoundError("No bold font found. Place Montserrat-Bold.ttf in static/fonts/.")

# ── Name placeholder (dotted line on left half of template) ─────────────────
# Pixel-measured from the 1240×1748 template:
#   Dotted-line band centre: x≈303, y≈423
#   Available width along the dotted line: x=124 to x=482 → 358 px
NAME_CENTER_X   = 303   # horizontal centre of dotted-line area
NAME_DOTTED_Y   = 423   # vertical centre of dotted-line band
NAME_MAX_WIDTH  = 358   # maximum text width in pixels before wrapping

# ── QR code — bottom-left corner ────────────────────────────────────────────
QR_SIZE         = 200
QR_MARGIN       = 45
QR_X            = QR_MARGIN
QR_Y            = 1748 - QR_SIZE - QR_MARGIN   # = 1503

# ── Card number — green, directly above QR code ─────────────────────────────
CARD_NUM_COLOR  = "#185a3f"   # dark green matching the template palette
CARD_NUM_SIZE   = 38          # font size for "NO. XXXX"
CARD_NUM_GAP    = 10          # gap (px) between bottom of number text and top of QR

# ---------------------------------------------------------------------------
# Supabase Storage Helpers
# ---------------------------------------------------------------------------

def upload_to_supabase(bucket: str, filename: str, data: bytes, content_type: str = "image/png") -> str:
    if not supabase:
        raise RuntimeError("Supabase client not initialized.")
    supabase.storage.from_(bucket).upload(
        path=filename,
        file=data,
        file_options={"content-type": content_type, "upsert": "true"},
    )
    return supabase.storage.from_(bucket).get_public_url(filename)


def delete_from_supabase(bucket: str, filename: str):
    if not supabase:
        return
    try:
        supabase.storage.from_(bucket).remove([filename])
    except Exception as e:
        logging.warning(f"Could not delete {filename} from {bucket}: {e}")


def download_from_supabase(bucket: str, filename: str) -> bytes:
    if not supabase:
        raise RuntimeError("Supabase client not initialized.")
    return supabase.storage.from_(bucket).download(filename)


def qr_filename_from_guest(guest) -> str:
    sanitized = get_safe_filename_name_part(guest.name or "GUEST")
    return f"{guest.qr_code_id}-{sanitized}.png"


def card_filename_from_guest(guest) -> str:
    sanitized = get_safe_filename_name_part(guest.name or "GUEST")
    return f"GUEST-{guest.visual_id:04d}-{sanitized}.png"


# ---------------------------------------------------------------------------
# QR Code Generation
# ---------------------------------------------------------------------------

def generate_qr_bytes(data: str) -> bytes:
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_H,
        box_size=10,
        border=4,
    )
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# ── UNIFIED CARD RENDERING HELPER ───────────────────────────────────────────
# ---------------------------------------------------------------------------

def _fit_name_font(draw, name: str, max_width: int) -> tuple[ImageFont.FreeTypeFont, list[str]]:
    """
    Return (font, lines) where lines fits within max_width.
    Starts at 44px and steps down until the longest line fits,
    or wraps to two lines if necessary.
    """
    font_path = _bold_font_path()
    for size in range(44, 19, -2):          # 44 → 22 px in steps of 2
        font = ImageFont.truetype(font_path, size)
        # Try single line first
        bbox = font.getbbox(name)
        if bbox[2] - bbox[0] <= max_width:
            return font, [name]
        # Try two lines
        wrapped = textwrap.fill(name, width=20)
        lines   = wrapped.split("\n")
        widths  = [(font.getbbox(l)[2] - font.getbbox(l)[0]) for l in lines]
        if max(widths) <= max_width:
            return font, lines
    # Last resort: return 22px with wrapping
    font = ImageFont.truetype(font_path, 22)
    wrapped = textwrap.fill(name, width=22)
    return font, wrapped.split("\n")


def _draw_card(guest, qr_img: Image.Image) -> Image.Image:
    """
    Render one guest invitation card.

    Layout:
      • Guest name  → bold font, black, centred on the dotted-line placeholder
                      (left half of template, y ≈ 423).  Font auto-sizes so the
                      name always fits within the 358 px dotted-line width.
      • QR code     → bottom-left corner (x=45, y=1503), 200×200 px.
      • Card number → green (#185a3f), directly above the QR code.
    """
    template_path = os.path.join("static", "Card Template.jpg")
    if not os.path.exists(template_path):
        raise FileNotFoundError(f"Card template not found: {template_path}")

    img  = Image.open(template_path).convert("RGB")
    draw = ImageDraw.Draw(img)

    font_path = _bold_font_path()
    num_font  = ImageFont.truetype(font_path, CARD_NUM_SIZE)

    # ── 1. Guest name on dotted-line placeholder ─────────────────────────
    raw_name      = (guest.name or "GUEST").upper()
    name_font, lines = _fit_name_font(draw, raw_name, NAME_MAX_WIDTH)

    # Measure total block height
    sample_bbox = name_font.getbbox("Ag")
    font_h      = sample_bbox[3] - sample_bbox[1]
    line_gap    = 6
    line_h      = font_h + line_gap
    total_h     = line_h * len(lines) - line_gap

    # Centre the block vertically on the dotted-line band
    start_y = NAME_DOTTED_Y - total_h // 2

    for i, line in enumerate(lines):
        bbox   = draw.textbbox((0, 0), line, font=name_font)
        text_w = bbox[2] - bbox[0]
        x      = NAME_CENTER_X - text_w // 2
        draw.text((x, start_y + i * line_h), line, font=name_font, fill="#000000")

    # ── 2. QR code — bottom-left ─────────────────────────────────────────
    qr_resized = qr_img.resize((QR_SIZE, QR_SIZE), Image.LANCZOS)
    img.paste(qr_resized, (QR_X, QR_Y))

    # ── 3. Card number — green, above QR ─────────────────────────────────
    vis_text    = f"NO. {guest.visual_id:04d}"
    num_bbox    = draw.textbbox((0, 0), vis_text, font=num_font)
    num_h       = num_bbox[3] - num_bbox[1]
    num_y       = QR_Y - num_h - CARD_NUM_GAP
    draw.text((QR_X, num_y), vis_text, font=num_font, fill=CARD_NUM_COLOR)

    return img


def _generate_card_bytes(guest) -> bytes | None:
    """Generate card image bytes for a guest (fetches QR from Supabase)."""
    try:
        qr_data = download_from_supabase(QR_BUCKET, qr_filename_from_guest(guest))
        qr_img  = Image.open(BytesIO(qr_data))
        img     = _draw_card(guest, qr_img)
        buf     = BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    except Exception as e:
        logging.error(f"_generate_card_bytes failed for {guest.name}: {e}")
        return None


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def to_whatsapp_number(phone):
    """Normalise any TZ phone number to 255XXXXXXXXX format."""
    phone = str(phone).strip()
    if phone.startswith('+'):
        phone = phone[1:]
    if phone.startswith('255') and len(phone) == 12:
        return phone
    if phone.startswith('0'):
        phone = phone[1:]
    if len(phone) == 9 and phone[0] in ('7', '6'):
        return f"255{phone}"
    if phone.startswith('255'):
        return phone
    return phone


app.jinja_env.globals.update(to_whatsapp_number=to_whatsapp_number, url_encode=url_encode)


def get_safe_filename_name_part(name):
    safe_name = (name or "").upper()
    return "".join(c if c.isalnum() else '_' for c in safe_name)


def normalize_card_type(card_type_input, allowed_input=None):
    card_type = (card_type_input or "").strip().lower()

    if card_type in ("s", "single"):
        return "single", 1
    if card_type in ("d", "double"):
        return "double", 2
    if card_type in ("f", "family", "group"):
        if allowed_input:
            try:
                allowed = int(allowed_input)
                if allowed >= 3:
                    return "family", allowed
                if allowed == 2:
                    return "double", 2
            except ValueError:
                pass
        return "family", 5

    if allowed_input:
        try:
            allowed = int(allowed_input)
            if allowed <= 1:
                return "single", 1
            if allowed == 2:
                return "double", 2
            return "family", allowed
        except ValueError:
            pass

    return "single", 1


def get_next_visual_id(db_session):
    max_id = db_session.query(func.max(Guest.visual_id)).scalar()
    return 1 if max_id is None else int(max_id) + 1

# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('logged_in'):
            flash('Please log in first.', 'warning')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route('/')
@login_required
def view_all():
    with get_db_session() as db:
        guests = db.query(Guest).order_by(Guest.visual_id).all()
        missing = [g for g in guests if g.visual_id is None]
        for g in missing:
            g.visual_id = get_next_visual_id(db)
            db.add(g)
        if missing:
            db.commit()
        return render_template('guests.html', guests=guests, current_environment=flask_env)


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        if request.form.get('username') == ADMIN_USERNAME and request.form.get('password') == ADMIN_PASSWORD:
            session['logged_in'] = True
            flash('Login successful.', 'success')
            return redirect(url_for('view_all'))
        flash('Invalid credentials.', 'danger')
    return render_template('login.html')


@app.route('/logout')
@login_required
def logout():
    session.pop('logged_in', None)
    flash('Logged out.', 'info')
    return redirect(url_for('login'))


# -------------------- add_guest --------------------
@app.route('/add_guest', methods=['GET', 'POST'])
@login_required
def add_guest():
    if request.method == 'POST':
        name = (request.form.get('name') or '').strip()
        phone = to_whatsapp_number((request.form.get('phone') or '').strip())
        card_type_input = request.form.get('card_type', 'single')
        group_size_input = request.form.get('group_size', '').strip()

        card_type, default_size = normalize_card_type(card_type_input, group_size_input)
        group_size = int(group_size_input) if (card_type == 'family' and group_size_input.isdigit()) else default_size

        with get_db_session() as db:
            if db.query(Guest).filter_by(phone=phone).first():
                flash(f"Guest with phone {phone} already exists.", "warning")
                return redirect(url_for('add_guest'))

            visual_id = get_next_visual_id(db)
            qr_id = f"GUEST-{visual_id:04d}"

            try:
                qr_bytes = generate_qr_bytes(qr_id)
                qr_fname = f"{qr_id}-{get_safe_filename_name_part(name or 'GUEST')}.png"
                qr_url = upload_to_supabase(QR_BUCKET, qr_fname, qr_bytes)
            except Exception as e:
                current_app.logger.warning(f"QR upload failed: {e}")
                qr_url = ""

            guest = Guest(
                name=name, phone=phone, qr_code_id=qr_id,
                qr_code_url=qr_url, visual_id=visual_id,
                card_type=card_type, group_size=group_size, checked_in_count=0
            )
            db.add(guest)
            db.commit()
            flash(f"Guest '{name or phone}' added. Card: {card_type.title()}, entries: {group_size}.", "success")
            return redirect(url_for('view_all'))

    return render_template('add_guest.html')


# -------------------- upload_csv --------------------
@app.route('/upload_csv', methods=['GET', 'POST'])
@login_required
def upload_csv():
    if request.method == 'POST':
        file = request.files.get('file')
        if not file or file.filename == '':
            flash("No file selected.", "danger")
            return redirect(request.url)

        stream = StringIO(file.stream.read().decode("utf-8"))
        reader = csv.DictReader(stream)
        added = skipped = 0

        def get_row(row, *keys):
            for k in keys:
                v = row.get(k) or row.get(k.lower()) or row.get(k.capitalize())
                if v:
                    return v.strip()
            return ""

        def normalize(raw):
            raw = (raw or "").strip().lower()
            if raw in ["s", "single"]: return "single"
            if raw in ["d", "double"]: return "double"
            if raw in ["f", "family", "group"]: return "family"
            return "single"

        with get_db_session() as db:
            for row in reader:
                name = get_row(row, "name", "Name")
                raw_phone = get_row(row, "phone", "Phone")
                if not raw_phone:
                    skipped += 1
                    continue

                phone = to_whatsapp_number(raw_phone)
                card_type = normalize(get_row(row, "Card Type", "card_type", "type"))

                if card_type == "single":
                    group_size = 1
                elif card_type == "double":
                    group_size = 2
                else:
                    try:
                        group_size = max(1, int(get_row(row, "Allowed", "allowed", "Size", "size", "Group Size", "group_size")))
                    except:
                        group_size = 1

                if db.query(Guest).filter_by(phone=phone).first():
                    skipped += 1
                    continue

                visual_id = get_next_visual_id(db)
                qr_id = f"GUEST-{visual_id:04d}"
                qr_fname = f"{qr_id}-{get_safe_filename_name_part(name or 'GUEST')}.png"

                try:
                    qr_bytes = generate_qr_bytes(qr_id)
                    qr_url = upload_to_supabase(QR_BUCKET, qr_fname, qr_bytes)
                except Exception as e:
                    current_app.logger.warning(f"QR upload failed for {name}: {e}")
                    qr_url = ""

                db.add(Guest(
                    name=name, phone=phone, qr_code_id=qr_id,
                    qr_code_url=qr_url, visual_id=visual_id,
                    card_type=card_type, group_size=group_size, checked_in_count=0
                ))
                db.flush()
                added += 1

            db.commit()

        flash(f"CSV processed — Added: {added}, Skipped: {skipped}", "success")
        return redirect(url_for('view_all'))

    return render_template('upload_csv.html')


# -------------------- update_status --------------------
@app.route('/update_status', methods=['POST'])
@login_required
def update_status():
    data = request.get_json() or {}
    qr_code_id = data.get("qr_code_id")
    if not qr_code_id:
        return jsonify(success=False, message="Missing qr_code_id.")

    with get_db_session() as db:
        try:
            guest = db.query(Guest).filter_by(qr_code_id=qr_code_id).first()
            if not guest:
                return jsonify(success=False, message="Guest not found.")

            remaining = guest.group_size - guest.checked_in_count
            if remaining <= 0:
                return jsonify(
                    success=False, already_entered=True,
                    message="All allowed entries have already checked in.",
                    guest={"visual_id": guest.visual_id, "name": guest.name,
                           "card_type": (guest.card_type or "").title(), "remaining_entries": 0}
                )

            guest.checked_in_count = (guest.checked_in_count or 0) + 1
            if guest.checked_in_count >= guest.group_size:
                guest.has_entered = True
                guest.entry_time = datetime.now()

            db.commit()
            return jsonify(
                success=True, message="Check-in successful.",
                guest={"visual_id": guest.visual_id, "name": guest.name,
                       "card_type": (guest.card_type or "").title(),
                       "remaining_entries": guest.group_size - guest.checked_in_count}
            )
        except Exception as e:
            db.rollback()
            current_app.logger.exception(f"Error updating status for {qr_code_id}: {e}")
            return jsonify(success=False, message=f"An error occurred: {e}")


@app.route('/search_guests')
@login_required
def search_guests():
    query = request.args.get('q', '').strip()
    with get_db_session() as db:
        if query:
            guests = db.query(Guest).filter(
                (Guest.name.ilike(f'%{query}%')) | (Guest.phone.ilike(f'%{query}%'))
            ).order_by(Guest.visual_id).all()
        else:
            guests = db.query(Guest).order_by(Guest.visual_id).all()

        return jsonify([{
            "visual_id": g.visual_id, "name": g.name, "phone": g.phone,
            "qr_code_url": g.qr_code_url, "has_entered": g.has_entered,
            "entry_time": g.entry_time.strftime('%Y-%m-%d %H:%M:%S') if g.entry_time else 'N/A',
            "card_type": g.card_type
        } for g in guests])


# -------------------- download_excel --------------------
@app.route('/download_excel')
@login_required
def download_excel():
    with get_db_session() as db:
        guests = db.query(Guest).all()

        def ct(g): return (g.card_type or "").strip().lower()

        total_guests = len(guests)
        single_cards = sum(1 for g in guests if ct(g) == "single")
        double_cards = sum(1 for g in guests if ct(g) == "double")
        family_cards = sum(1 for g in guests if ct(g) == "family")
        total_family_allowed = sum(g.group_size for g in guests if ct(g) == "family")
        entered_guests = sum(1 for g in guests if bool(g.has_entered))

        wb = Workbook()
        ws = wb.active
        ws.title = "Guest Report"
        ws["A1"] = "Guest Summary Report"
        ws["A1"].font = Font(size=14, bold=True)

        summary_data = [
            ("Total Guests", total_guests), ("Single Cards", single_cards),
            ("Double Cards", double_cards), ("Family Cards", family_cards),
            ("Total Allowed by Family Cards", total_family_allowed),
            ("Guests Entered", entered_guests), ("Guests Not Entered", total_guests - entered_guests),
        ]

        row = 3
        for label, value in summary_data:
            ws[f"A{row}"] = label
            ws[f"B{row}"] = value
            ws[f"A{row}"].font = Font(bold=True)
            row += 1

        table_start = row + 1
        headers = ["ID", "Name", "Phone", "QR Code ID", "Has Entered", "Entry Time",
                   "Visual ID", "Card Type", "Group Size", "WhatsApp", "RSVP", "AT SMS Sent"]
        for col, header in enumerate(headers, start=1):
            ws.cell(row=table_start, column=col, value=header).font = Font(bold=True)

        for i, g in enumerate(guests, start=table_start + 1):
            ws.cell(i, 1, g.id); ws.cell(i, 2, g.name); ws.cell(i, 3, g.phone)
            ws.cell(i, 4, g.qr_code_id)
            ws.cell(i, 5, "Entered" if g.has_entered else "Not Entered")
            ws.cell(i, 6, g.entry_time.strftime('%Y-%m-%d %H:%M:%S') if g.entry_time else "")
            ws.cell(i, 7, g.visual_id); ws.cell(i, 8, g.card_type); ws.cell(i, 9, g.group_size)
            ws.cell(i, 10, "Yes" if g.has_whatsapp else ("No" if g.has_whatsapp is False else "Unknown"))
            ws.cell(i, 11, g.rsvp_status or "—")
            ws.cell(i, 12, "Yes" if g.at_sms_sent else "No")

        first_data_row = table_start + 1
        last_data_row = table_start + len(guests)
        if last_data_row >= first_data_row:
            rng = f"E{first_data_row}:E{last_data_row}"
            ws.conditional_formatting.add(rng, CellIsRule(operator="equal", formula=['"Entered"'],
                fill=PatternFill(start_color="C6EFCE", end_color="C6EFCE", fill_type="solid")))
            ws.conditional_formatting.add(rng, CellIsRule(operator="equal", formula=['"Not Entered"'],
                fill=PatternFill(start_color="FFC7CE", end_color="FFC7CE", fill_type="solid")))

        for column in ws.columns:
            max_length = max((len(str(cell.value)) for cell in column if cell.value), default=0)
            ws.column_dimensions[column[0].column_letter].width = max_length + 2

        output = BytesIO()
        wb.save(output)
        output.seek(0)
        return send_file(output, as_attachment=True, download_name="guest_report.xlsx",
                         mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


# -------------------- zip_qr_codes_web --------------------
@app.route('/zip_qr_codes_web')
@login_required
def zip_qr_codes_web():
    with get_db_session() as db:
        guests = db.query(Guest).all()

    memory_file = BytesIO()
    with zipfile.ZipFile(memory_file, 'w') as zf:
        for guest in guests:
            if not guest.qr_code_url:
                continue
            try:
                fname = qr_filename_from_guest(guest)
                data = download_from_supabase(QR_BUCKET, fname)
                zf.writestr(fname, data)
            except Exception as e:
                current_app.logger.warning(f"Could not fetch QR for {guest.name}: {e}")

    memory_file.seek(0)
    return send_file(memory_file, download_name='qr_codes.zip', as_attachment=True, mimetype='application/zip')


# -------------------- edit_guest --------------------
@app.route('/edit_guest/<int:guest_id>', methods=['GET', 'POST'])
@login_required
def edit_guest(guest_id):
    with get_db_session() as db:
        try:
            guest = db.get(Guest, guest_id)
            if not guest:
                flash("Guest not found.", "danger")
                return redirect(url_for('view_all'))

            if request.method == 'POST':
                guest.name = request.form.get('name', guest.name).strip()
                guest.phone = to_whatsapp_number(request.form.get('phone', guest.phone))
                guest.has_entered = 'has_entered' in request.form

                new_card_type_raw = request.form.get('card_type', guest.card_type)
                group_size_raw = request.form.get('group_size', '').strip()
                new_card_type, _ = normalize_card_type(new_card_type_raw, group_size_raw or None)

                if new_card_type == "family":
                    try:
                        new_group_size = max(1, int(request.form.get('group_size', '').strip()))
                    except:
                        flash("Invalid group size for family card.", "danger")
                        return redirect(request.url)
                    if new_group_size < guest.checked_in_count:
                        flash(f"Group size cannot be less than checked-in count ({guest.checked_in_count}).", "danger")
                        return redirect(request.url)
                elif new_card_type == "single":
                    new_group_size = 1
                    if guest.checked_in_count > 1:
                        flash("Guest already used more scans than a single card allows.", "danger")
                        return redirect(request.url)
                elif new_card_type == "double":
                    new_group_size = 2
                    if guest.checked_in_count > 2:
                        flash("Guest already used more scans than a double card allows.", "danger")
                        return redirect(request.url)
                else:
                    flash("Unknown card type.", "danger")
                    return redirect(request.url)

                guest.card_type = new_card_type
                guest.group_size = new_group_size
                db.commit()
                flash('Guest updated successfully.', 'success')
                return redirect(url_for('view_all'))

            return render_template('edit_guest.html', guest=guest)
        except Exception as e:
            db.rollback()
            flash(f'Error updating guest: {e}', 'danger')
            current_app.logger.error(f"Error updating guest {guest_id}: {e}", exc_info=True)
            return redirect(url_for('view_all'))


@app.route('/scan_qr')
@login_required
def scan_qr():
    return render_template('scan_qr.html')


# -------------------- delete_guest --------------------
@app.route('/delete_guest/<int:guest_id>', methods=['GET'])
@login_required
def delete_guest(guest_id):
    with get_db_session() as db:
        try:
            guest = db.get(Guest, guest_id)
            if not guest:
                flash("Guest not found.", "danger")
                return redirect(url_for('view_all'))

            delete_from_supabase(QR_BUCKET, qr_filename_from_guest(guest))
            delete_from_supabase(CARDS_BUCKET, card_filename_from_guest(guest))

            db.delete(guest)
            db.commit()
            flash('Guest and associated files deleted.', 'success')
        except Exception as e:
            db.rollback()
            flash(f'Error deleting guest: {e}', 'danger')
            current_app.logger.error(f"Error deleting guest {guest_id}: {e}", exc_info=True)

    return redirect(url_for('view_all'))


# -------------------- regenerate_qr_codes --------------------
@app.route('/regenerate_qr_codes')
@login_required
def regenerate_qr_codes():
    with get_db_session() as db:
        try:
            guests = db.query(Guest).all()
            for guest in guests:
                if guest.visual_id is None:
                    guest.visual_id = get_next_visual_id(db)
                qr_id = f"GUEST-{guest.visual_id:04d}"
                qr_fname = qr_filename_from_guest(guest)
                qr_bytes = generate_qr_bytes(qr_id)
                qr_url = upload_to_supabase(QR_BUCKET, qr_fname, qr_bytes)
                guest.qr_code_id = qr_id
                guest.qr_code_url = qr_url
            db.commit()
            flash("QR codes regenerated.", "success")
        except Exception as e:
            db.rollback()
            flash(f"Error regenerating QR codes: {e}", "danger")
            current_app.logger.error(f"Error regenerating QR codes: {e}", exc_info=True)

    return redirect(url_for('view_all'))


# -------------------- generate_guest_cards --------------------
@app.route('/generate_guest_cards')
@login_required
def generate_guest_cards():
    template_path = os.path.join("static", "Card Template.jpg")
    if not os.path.exists(template_path):
        flash("Card template not found at static/Card Template.jpg", "danger")
        return redirect(url_for('view_all'))

    try:
        _bold_font_path()   # raises if no font found
    except FileNotFoundError as e:
        flash(str(e), "danger")
        return redirect(url_for('view_all'))

    with get_db_session() as db:
        guests = db.query(Guest).all()

        for guest in guests:
            try:
                if not guest.qr_code_url:
                    flash(f"No QR URL for {guest.name}. Skipping.", "warning")
                    continue

                qr_data = download_from_supabase(QR_BUCKET, qr_filename_from_guest(guest))
                qr_img  = Image.open(BytesIO(qr_data))

                img     = _draw_card(guest, qr_img)
                buf     = BytesIO()
                img.save(buf, format="PNG")
                card_bytes = buf.getvalue()

                upload_to_supabase(CARDS_BUCKET, card_filename_from_guest(guest), card_bytes)

            except Exception as e:
                flash(f"Failed card for {guest.name}: {e}", "danger")
                current_app.logger.error(
                    f"Card gen error for guest {guest.visual_id}: {e}", exc_info=True
                )

    flash("Guest invitation cards generated successfully.", "success")
    return redirect(url_for('view_all'))


# -------------------- download_card_by_id --------------------
@app.route('/download_card_by_id/<int:visual_id>')
@login_required
def download_card_by_id(visual_id):
    with get_db_session() as db:
        try:
            guest = db.query(Guest).filter_by(visual_id=visual_id).first()
            if not guest:
                flash("Guest not found.", "danger")
                return redirect(url_for('view_all'))

            template_path = os.path.join("static", "Card Template.jpg")
            if not os.path.exists(template_path):
                flash("Card template missing.", "danger")
                return redirect(url_for('view_all'))

            qr_data = download_from_supabase(QR_BUCKET, qr_filename_from_guest(guest))
            qr_img  = Image.open(BytesIO(qr_data))

            img = _draw_card(guest, qr_img)
            buf = BytesIO()
            img.save(buf, format="PNG")
            buf.seek(0)
            return send_file(
                buf,
                as_attachment=True,
                download_name=f"Guest-{guest.visual_id:04d}.png",
                mimetype="image/png",
            )

        except Exception as e:
            flash(f"Error generating card: {e}", "danger")
            current_app.logger.error(f"Error downloading card: {e}", exc_info=True)
            return redirect(url_for('view_all'))


# -------------------- download_all_cards --------------------
@app.route('/download_all_cards')
@login_required
def download_all_cards():
    with get_db_session() as db:
        guests = db.query(Guest).all()

    zip_buffer = BytesIO()
    count = 0
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
        for guest in guests:
            try:
                fname = card_filename_from_guest(guest)
                data = download_from_supabase(CARDS_BUCKET, fname)
                zf.writestr(fname, data)
                count += 1
            except Exception as e:
                current_app.logger.warning(f"Could not fetch card for {guest.name}: {e}")

    if count == 0:
        flash("No invitation cards found. Please generate them first.", "warning")
        return redirect(url_for('view_all'))

    zip_buffer.seek(0)
    return send_file(zip_buffer, download_name="invitation_cards.zip", as_attachment=True)


# -------------------- guest_report --------------------
@app.route('/guest_report_data')
@login_required
def guest_report_data():
    with get_db_session() as db:
        total = db.query(Guest).count()
        return jsonify({
            "total_guests": total,
            "single_cards": db.query(Guest).filter_by(card_type='single').count(),
            "double_cards": db.query(Guest).filter_by(card_type='double').count(),
            "family_cards": db.query(Guest).filter_by(card_type='family').count(),
            "entered_guests": db.query(Guest).filter_by(has_entered=True).count(),
            "not_entered_guests": total - db.query(Guest).filter_by(has_entered=True).count(),
        })


@app.route('/guest_report')
@login_required
def guest_report():
    return render_template('guest_report.html')


# -------------------- clear_all_data --------------------
@app.route('/clear_all_data', methods=['GET'])
@login_required
def clear_all_data():
    with get_db_session() as db:
        try:
            guests = db.query(Guest).all()

            for guest in guests:
                delete_from_supabase(QR_BUCKET, qr_filename_from_guest(guest))
                delete_from_supabase(CARDS_BUCKET, card_filename_from_guest(guest))

            num_deleted = db.query(Guest).delete()
            db.commit()
            flash(f"Successfully deleted {num_deleted} guests.", "success")
        except Exception as e:
            db.rollback()
            flash(f"An error occurred while clearing data: {e}", "danger")
            current_app.logger.error(f"Error clearing all data: {e}", exc_info=True)

    return redirect(url_for('view_all'))


# -------------------- WEBHOOK (RSVP receiver) --------------------

@app.route('/webhook/whatsapp', methods=['GET', 'POST'])
def whatsapp_webhook():
    if request.method == 'GET':
        mode = request.args.get('hub.mode')
        token = request.args.get('hub.verify_token')
        challenge = request.args.get('hub.challenge')

        if mode == 'subscribe' and token == WHATSAPP_VERIFY_TOKEN:
            current_app.logger.info("Webhook verified by Meta.")
            return challenge, 200
        return "Forbidden", 403

    try:
        data = request.get_json()
        current_app.logger.info(f"Webhook payload: {data}")

        entries = data.get('entry', [])
        for entry in entries:
            for change in entry.get('changes', []):
                value = change.get('value', {})
                messages = value.get('messages', [])

                for msg in messages:
                    msg_type = msg.get('type')
                    from_number = msg.get('from')

                    if msg_type == 'button':
                        button_text = msg.get('button', {}).get('text', '').strip()
                        _handle_rsvp(from_number, button_text)

                    elif msg_type == 'interactive':
                        interactive = msg.get('interactive', {})
                        if interactive.get('type') == 'button_reply':
                            button_text = interactive.get('button_reply', {}).get('title', '').strip()
                            _handle_rsvp(from_number, button_text)

    except Exception as e:
        current_app.logger.error(f"Webhook processing error: {e}", exc_info=True)

    return jsonify({"status": "ok"}), 200


def _handle_rsvp(from_number: str, button_text: str):
    """Match incoming button tap to a guest and save RSVP status."""
    button_lower = button_text.lower()
    if any(x in button_lower for x in ['nitakuwepo', "i'll be there", 'attending']):
        rsvp_status = 'attending'
    elif any(x in button_lower for x in ['sitakuwepo', "can't make it", 'not attending']):
        rsvp_status = 'not_attending'
    else:
        current_app.logger.warning(f"Unknown button text from {from_number}: {button_text}")
        return

    raw = str(from_number).strip().lstrip('+')
    variants = {raw, f"+{raw}"}
    if raw.startswith("255") and len(raw) >= 11:
        local9 = raw[3:]
        variants.update({local9, f"0{local9}", f"+255{local9}"})
    if raw.startswith("0") and len(raw) == 10:
        local9 = raw[1:]
        variants.update({f"255{local9}", f"+255{local9}", local9})

    current_app.logger.info(f"RSVP lookup for {from_number}, trying variants: {variants}")

    with get_db_session() as db:
        guest = db.query(Guest).filter(Guest.phone.in_(variants)).first()

        if not guest:
            current_app.logger.warning(
                f"No guest found for number: {from_number} (tried variants: {variants})"
            )
            return

        guest.rsvp_status = rsvp_status
        guest.rsvp_at = datetime.now()
        db.commit()
        current_app.logger.info(
            f"RSVP saved: {guest.name} → {rsvp_status} (from {from_number}, matched '{guest.phone}')"
        )


# ════════════════════════════════════════════════════════════════════════════
#  UNIFIED SEND ENGINE
# ════════════════════════════════════════════════════════════════════════════

def _send_to_guest(guest, db):
    """
    Core delivery logic for one guest.

    Flow:
        1. Attempt WhatsApp.
        2. If Meta returns invalid_number  →  mark WA failed, auto-send SMS.
        3. If WA succeeds                  →  mark WA sent, skip SMS
           (SMS can still be sent manually / via separate bulk action).
        4. If WA fails for any OTHER reason →  mark WA failed, attempt SMS.

    Returns a dict:
        {
            wa:      "sent" | "skipped" | "failed" | "invalid",
            sms:     "sent" | "skipped" | "failed" | "not_configured",
            overall: "success" | "partial" | "failed",
            message: str
        }
    """
    now = datetime.now()
    wa_status  = "skipped"
    sms_status = "skipped"
    messages   = []

    phone = to_whatsapp_number(guest.phone)
    if not phone:
        return {
            "wa": "failed", "sms": "failed",
            "overall": "failed",
            "message": "No valid phone number."
        }

    # ── Step 1: try WhatsApp ──────────────────────────────────────────────
    try:
        card_fname = card_filename_from_guest(guest)
        try:
            card_bytes = download_from_supabase(CARDS_BUCKET, card_fname)
        except Exception:
            card_bytes = _generate_card_bytes(guest)
            if card_bytes:
                upload_to_supabase(CARDS_BUCKET, card_fname, card_bytes)

        if not card_bytes:
            raise ValueError("Could not retrieve or generate card image.")

        wa_result = send_guest_card(
            to=phone,
            guest_name=guest.name or "Guest",
            visual_id=guest.visual_id,
            card_type=guest.card_type,
            image_bytes=card_bytes,
            filename=card_fname,
        )

        if wa_result.get("status") == "invalid_number":
            guest.has_whatsapp        = False
            guest.whatsapp_checked_at = now
            guest.whatsapp_sent       = False
            guest.whatsapp_error      = "Not on WhatsApp"
            wa_status = "invalid"
            messages.append("WhatsApp: not on platform.")
        else:
            guest.whatsapp_sent    = True
            guest.whatsapp_sent_at = now
            guest.whatsapp_error   = None
            wa_status = "sent"
            messages.append("WhatsApp: sent.")

    except Exception as e:
        err_str = str(e)[:500]
        guest.whatsapp_sent  = False
        guest.whatsapp_error = err_str
        wa_status = "failed"
        messages.append(f"WhatsApp failed: {err_str}")
        logging.error(f"WA send failed for {guest.name}: {e}", exc_info=True)

    # ── Step 2: SMS via Africa's Talking ─────────────────────────────────
    # Set should_send_sms = True  to always send both WA and SMS.
    # Set should_send_sms = (wa_status != "sent")  for SMS-as-fallback only.
    should_send_sms = True

    if should_send_sms:
        if not at_configured():
            sms_status = "not_configured"
            messages.append("SMS: Africa's Talking not configured.")
        else:
            try:
                sms_result = at_send_sms(phone, build_sms_message(guest))
                if sms_result.get("success"):
                    guest.at_sms_sent    = True
                    guest.at_sms_error   = None
                    guest.at_sms_sent_at = now
                    sms_status = "sent"
                    messages.append("SMS: sent.")
                else:
                    err_str = sms_result.get("error", "Unknown SMS error")[:500]
                    guest.at_sms_sent  = False
                    guest.at_sms_error = err_str
                    sms_status = "failed"
                    messages.append(f"SMS failed: {err_str}")
            except Exception as e:
                err_str = str(e)[:500]
                guest.at_sms_sent  = False
                guest.at_sms_error = err_str
                sms_status = "failed"
                messages.append(f"SMS error: {err_str}")
                logging.error(f"SMS send failed for {guest.name}: {e}", exc_info=True)
    else:
        sms_status = "skipped"

    db.commit()

    # ── Determine overall outcome ─────────────────────────────────────────
    if wa_status == "sent" or sms_status == "sent":
        overall = "success"
    elif wa_status in ("failed", "invalid") and sms_status in ("failed", "not_configured"):
        overall = "failed"
    else:
        overall = "partial"

    return {
        "wa":      wa_status,
        "sms":     sms_status,
        "overall": overall,
        "message": " | ".join(messages),
    }


# ── Unified single ────────────────────────────────────────────────────────────
@app.route('/send_unified_single/<int:guest_id>', methods=['POST'])
@login_required
def send_unified_single(guest_id):
    with get_db_session() as db:
        guest = db.get(Guest, guest_id)
        if not guest:
            return jsonify(success=False, message="Guest not found.")
        result = _send_to_guest(guest, db)
        return jsonify(
            success=(result["overall"] != "failed"),
            overall=result["overall"],
            wa=result["wa"],
            sms=result["sms"],
            message=result["message"],
            guest_id=guest_id,
        )


# ── Unified bulk ──────────────────────────────────────────────────────────────
@app.route('/send_unified_bulk', methods=['POST'])
@login_required
def send_unified_bulk():
    data   = request.get_json() or {}
    resend = data.get('resend', False)

    with get_db_session() as db:
        if resend:
            guests = db.query(Guest).order_by(Guest.visual_id).all()
        else:
            # Pending = WA not sent AND AT SMS not sent
            guests = db.query(Guest).filter(
                ((Guest.whatsapp_sent == False) | (Guest.whatsapp_sent == None)) &
                ((Guest.at_sms_sent == False) | (Guest.at_sms_sent == None))
            ).order_by(Guest.visual_id).all()

        totals = {
            "total":      len(guests),
            "wa_sent":    0, "wa_failed":  0,
            "sms_sent":   0, "sms_failed": 0,
            "errors":     [],
        }

        for guest in guests:
            result = _send_to_guest(guest, db)
            if result["wa"]  == "sent":                        totals["wa_sent"]   += 1
            elif result["wa"] in ("failed", "invalid"):        totals["wa_failed"] += 1
            if result["sms"] == "sent":                        totals["sms_sent"]  += 1
            elif result["sms"] == "failed":                    totals["sms_failed"] += 1
            if result["overall"] == "failed":
                totals["errors"].append({"name": guest.name, "error": result["message"]})
            time.sleep(0.1)   # avoid provider rate limits

        return jsonify(totals)


# -------------------- send_cards --------------------
@app.route('/send_cards', methods=['GET'])
@login_required
def send_cards():
    with get_db_session() as db:
        guests = db.query(Guest).order_by(Guest.visual_id).all()

        total         = len(guests)
        sent          = sum(1 for g in guests if g.whatsapp_sent)
        failed        = sum(1 for g in guests if g.whatsapp_error and not g.whatsapp_sent)
        pending       = total - sent
        attending     = sum(1 for g in guests if g.rsvp_status == 'attending')
        not_attending = sum(1 for g in guests if g.rsvp_status == 'not_attending')
        no_rsvp       = total - attending - not_attending

        # Africa's Talking SMS stats
        at_sms_sent_count   = sum(1 for g in guests if g.at_sms_sent)
        at_sms_failed_count = sum(1 for g in guests if g.at_sms_error and not g.at_sms_sent)

        wa_checked        = sum(1 for g in guests if g.has_whatsapp is not None)
        no_whatsapp_count = sum(1 for g in guests if g.has_whatsapp is False)

        return render_template(
            'send_cards.html',
            guests=guests,
            total=total,
            sent=sent,
            failed=failed,
            pending=pending,
            attending=attending,
            not_attending=not_attending,
            no_rsvp=no_rsvp,
            wa_checked=wa_checked,
            no_whatsapp_count=no_whatsapp_count,
            # Africa's Talking SMS
            at_configured=at_configured(),
            at_sms_sent_count=at_sms_sent_count,
            at_sms_failed_count=at_sms_failed_count,
            # Legacy aliases so old template refs don't break
            sms_enabled=at_configured(),
            sms_sent_count=at_sms_sent_count,
        )


# -------------------- send_card_single (legacy WA-only alias) --------------------
@app.route('/send_card_single/<int:guest_id>', methods=['POST'])
@login_required
def send_card_single(guest_id):
    with get_db_session() as db:
        guest = db.get(Guest, guest_id)
        if not guest:
            return jsonify(success=False, message="Guest not found.")
        result = _send_to_guest(guest, db)
        return jsonify(
            success=(result["wa"] == "sent"),
            message=result["message"],
            guest_id=guest_id,
        )


# -------------------- send_cards_bulk (legacy WA-only alias) --------------------
@app.route('/send_cards_bulk', methods=['POST'])
@login_required
def send_cards_bulk():
    data   = request.get_json() or {}
    resend = data.get('resend', False)

    with get_db_session() as db:
        if resend:
            guests = db.query(Guest).order_by(Guest.visual_id).all()
        else:
            guests = db.query(Guest).filter(
                (Guest.whatsapp_sent == False) | (Guest.whatsapp_sent == None)
            ).order_by(Guest.visual_id).all()

        sent = failed = 0
        errors = []
        for guest in guests:
            result = _send_to_guest(guest, db)
            if result["wa"] == "sent":
                sent += 1
            else:
                failed += 1
                if result["overall"] == "failed":
                    errors.append({"name": guest.name, "error": result["message"]})
            time.sleep(0.1)

        return jsonify(total=len(guests), sent=sent, failed=failed, errors=errors)


# ── Africa's Talking SMS — single ────────────────────────────────────────────
@app.route('/send_at_sms_single/<int:guest_id>', methods=['POST'])
@login_required
def send_at_sms_single(guest_id):
    """SMS-only send via Africa's Talking (skips WhatsApp)."""
    with get_db_session() as db:
        guest = db.get(Guest, guest_id)
        if not guest:
            return jsonify(success=False, message="Guest not found."), 404

        if not at_configured():
            return jsonify(success=False, message="Africa's Talking SMS not configured.")

        phone  = to_whatsapp_number(guest.phone)
        result = at_send_sms(phone, build_sms_message(guest))

        if result["success"]:
            guest.at_sms_sent    = True
            guest.at_sms_error   = None
            guest.at_sms_sent_at = datetime.now()
            db.commit()
            return jsonify(success=True)
        else:
            guest.at_sms_sent  = False
            guest.at_sms_error = result.get("error")
            db.commit()
            return jsonify(success=False, message=result.get("error"))


# ── Africa's Talking SMS — bulk ───────────────────────────────────────────────
@app.route('/send_at_sms_bulk', methods=['POST'])
@login_required
def send_at_sms_bulk():
    """SMS-only bulk send via Africa's Talking."""
    data   = request.get_json() or {}
    resend = data.get("resend", False)

    with get_db_session() as db:
        if resend:
            guests = db.query(Guest).all()
        else:
            guests = db.query(Guest).filter(
                (Guest.at_sms_sent == None) | (Guest.at_sms_sent == False)
            ).all()

        sent_count = failed_count = 0
        errors = []
        for guest in guests:
            phone  = to_whatsapp_number(guest.phone)
            result = at_send_sms(phone, build_sms_message(guest))
            if result["success"]:
                guest.at_sms_sent    = True
                guest.at_sms_error   = None
                guest.at_sms_sent_at = datetime.now()
                sent_count += 1
            else:
                guest.at_sms_sent  = False
                guest.at_sms_error = result.get("error")
                failed_count += 1
                errors.append({"name": guest.name, "error": result.get("error")})
            db.commit()
            time.sleep(0.1)

        return jsonify(total=len(guests), sent=sent_count, failed=failed_count, errors=errors)


# ---------------------------------------------------------------------------
# SMS message builder
# ---------------------------------------------------------------------------

def build_sms_message(guest) -> str:
    return (
        f"Habari {guest.name}, Karibu tusherekee siku hii ya furaha pamoja. "
        f"Namba yako ya kadi:  {guest.visual_id:04d}. "
        f"Karibu sana."
    )


# ---------------------------------------------------------------------------
# Misc routes
# ---------------------------------------------------------------------------

@app.route("/privacy")
def privacy():
    return render_template("privacy.html")


@app.route("/data-deletion")
def data_deletion():
    return """
    <html>
    <head><title>Data Deletion - SwiftInvite</title></head>
    <body style="font-family: Arial; margin: 40px;">
    <h1>User Data Deletion</h1>
    <p>If you would like to delete your data from SwiftInvite, please follow the instructions below:</p>
    <ol>
        <li>Send an email to: <strong>swiftinvite25@gmail.com</strong></li>
        <li>Include your phone number or identifier used in the app</li>
        <li>We will process your request within 7 days</li>
    </ol>
    <p>Alternatively, you may contact us directly for assistance.</p>
    </body>
    </html>
    """


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)