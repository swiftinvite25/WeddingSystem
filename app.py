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
WHATSAPP_ACCESS_TOKEN    = os.getenv("WHATSAPP_ACCESS_TOKEN")
WHATSAPP_VERIFY_TOKEN    = os.getenv("WHATSAPP_VERIFY_TOKEN", "wedding_webhook_secret")

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
    DB_FILE      = os.getenv("DB_FILE", "guests.db")
    DATABASE_URL = f"sqlite:///./{DB_FILE}"

if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

# ---------------------------------------------------------------------------
# Supabase Storage Configuration
# ---------------------------------------------------------------------------

SUPABASE_URL  = os.getenv("SUPABASE_URL")
SUPABASE_KEY  = os.getenv("SUPABASE_SERVICE_KEY")
QR_BUCKET     = os.getenv("SUPABASE_QR_BUCKET",    "qr-codes")
CARDS_BUCKET  = os.getenv("SUPABASE_CARDS_BUCKET", "guest-cards")

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
# Card rendering constants (all pixel values for 1240 × 1748 px template)
# ---------------------------------------------------------------------------

_MONTSERRAT_PATH = os.path.join("static", "fonts", "Montserrat-Bold.ttf")
_POPPINS_PATH    = "/usr/share/fonts/truetype/google-fonts/Poppins-Bold.ttf"
_ROBOTO_PATH     = os.path.join("static", "fonts", "Roboto-Bold.ttf")

def _bold_font_path() -> str:
    for p in (_MONTSERRAT_PATH, _ROBOTO_PATH, _POPPINS_PATH):
        if os.path.exists(p):
            return p
    raise FileNotFoundError("No bold font found. Place Montserrat-Bold.ttf in static/fonts/.")

# ── Name placeholder (dotted line on left half of template) ──────────────────
NAME_CENTER_X  = 303   # horizontal centre of dotted-line area
NAME_DOTTED_Y  = 423   # vertical centre of dotted-line band
NAME_MAX_WIDTH = 358   # maximum text width in pixels before wrapping

# ── QR code — bottom-left corner ─────────────────────────────────────────────
QR_SIZE   = 200
QR_MARGIN = 45
QR_X      = QR_MARGIN
QR_Y      = 1748 - QR_SIZE - QR_MARGIN   # = 1503

# ── Card number — top-left corner of the card ────────────────────────────────
CARD_NUM_COLOR  = "#185a3f"
CARD_NUM_SIZE   = 38
CARD_NUM_TOP_X  = 45
CARD_NUM_TOP_Y  = 45

# ── Card type label — above the QR code ──────────────────────────────────────
CARD_TYPE_COLOR = "#185a3f"
CARD_TYPE_SIZE  = 36
CARD_TYPE_GAP   = 10

# ---------------------------------------------------------------------------
# Supabase Storage Helpers
# ---------------------------------------------------------------------------

def upload_to_supabase(bucket: str, filename: str, data: bytes,
                       content_type: str = "image/jpeg") -> str:
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
    # NOTE: switched to .jpg — faster save, no fileno() PIL bug
    sanitized = get_safe_filename_name_part(guest.name or "GUEST")
    return f"GUEST-{guest.visual_id:04d}-{sanitized}.jpg"

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
    buf.seek(0)
    return buf.getvalue()

# ---------------------------------------------------------------------------
# Card Rendering
# ---------------------------------------------------------------------------

def _fit_name_font(draw, name: str, max_width: int):
    font_path = _bold_font_path()
    for size in range(44, 19, -2):
        font = ImageFont.truetype(font_path, size)
        bbox = font.getbbox(name)
        if bbox[2] - bbox[0] <= max_width:
            return font, [name]
        wrapped = textwrap.fill(name, width=20)
        lines   = wrapped.split("\n")
        widths  = [(font.getbbox(l)[2] - font.getbbox(l)[0]) for l in lines]
        if max(widths) <= max_width:
            return font, lines
    font    = ImageFont.truetype(font_path, 22)
    wrapped = textwrap.fill(name, width=22)
    return font, wrapped.split("\n")


def _draw_card(guest, qr_img: Image.Image) -> Image.Image:
    """
    Render one guest invitation card.

    Layout:
    • Card number  → top-left corner (x=45, y=45), dark green.
    • Guest name   → bold, black, centred on dotted-line placeholder (y≈423).
    • QR code      → bottom-left (x=45, y=1503), 200×200 px.
    • Card type    → dark green, directly above the QR code.
    """
    template_path = os.path.join("static", "Card Template.jpg")
    if not os.path.exists(template_path):
        raise FileNotFoundError(f"Card template not found: {template_path}")

    img  = Image.open(template_path).convert("RGB")
    draw = ImageDraw.Draw(img)
    font_path = _bold_font_path()

    num_font  = ImageFont.truetype(font_path, CARD_NUM_SIZE)
    type_font = ImageFont.truetype(font_path, CARD_TYPE_SIZE)

    # ── 1. Card number — top-left ─────────────────────────────────────────
    vis_text = f"NO. {guest.visual_id:04d}"
    draw.text(
        (CARD_NUM_TOP_X, CARD_NUM_TOP_Y),
        vis_text,
        font=num_font,
        fill=CARD_NUM_COLOR,
    )

    # ── 2. Guest name — on dotted-line placeholder ────────────────────────
    raw_name  = (guest.name or "GUEST").upper()
    name_font, lines = _fit_name_font(draw, raw_name, NAME_MAX_WIDTH)

    sample_bbox = name_font.getbbox("Ag")
    font_h      = sample_bbox[3] - sample_bbox[1]
    line_gap    = 6
    line_h      = font_h + line_gap
    total_h     = line_h * len(lines) - line_gap
    block_top_y = NAME_DOTTED_Y - total_h // 2

    for i, line in enumerate(lines):
        bbox   = draw.textbbox((0, 0), line, font=name_font)
        text_w = bbox[2] - bbox[0]
        x      = NAME_CENTER_X - text_w // 2
        y      = block_top_y + i * line_h
        draw.text((x, y), line, font=name_font, fill="#000000")

    # ── 3. QR code — bottom-left ──────────────────────────────────────────
    qr_resized = qr_img.resize((QR_SIZE, QR_SIZE), Image.LANCZOS)
    img.paste(qr_resized, (QR_X, QR_Y))

    # ── 4. Card type label — above QR ────────────────────────────────────
    type_label = (guest.card_type or "SINGLE").upper()
    type_bbox  = draw.textbbox((0, 0), type_label, font=type_font)
    type_h     = type_bbox[3] - type_bbox[1]
    type_y     = QR_Y - type_h - CARD_TYPE_GAP
    draw.text((QR_X, type_y), type_label, font=type_font, fill=CARD_TYPE_COLOR)

    return img


def _render_and_upload_card(guest) -> bool:
    """
    Generate card for one guest and upload to Supabase.
    Returns True on success, False on failure.
    Uses JPEG to avoid Pillow PNG/BytesIO fileno() bug and save memory.
    """
    try:
        qr_data = download_from_supabase(QR_BUCKET, qr_filename_from_guest(guest))
        qr_img  = Image.open(BytesIO(qr_data))
        img     = _draw_card(guest, qr_img)

        buf = BytesIO()
        img.save(buf, format="JPEG", quality=92)
        buf.seek(0)
        card_bytes = buf.getvalue()

        upload_to_supabase(
            CARDS_BUCKET,
            card_filename_from_guest(guest),
            card_bytes,
            content_type="image/jpeg",
        )
        return True
    except Exception as e:
        logging.error(f"_render_and_upload_card failed for {guest.name}: {e}")
        return False

# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def to_whatsapp_number(phone):
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
            if allowed <= 1: return "single", 1
            if allowed == 2: return "double", 2
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
        guests  = db.query(Guest).order_by(Guest.visual_id).all()
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
        if (request.form.get('username') == ADMIN_USERNAME and
                request.form.get('password') == ADMIN_PASSWORD):
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
        name             = (request.form.get('name') or '').strip()
        phone            = to_whatsapp_number((request.form.get('phone') or '').strip())
        card_type_input  = request.form.get('card_type', 'single')
        group_size_input = request.form.get('group_size', '').strip()
        card_type, default_size = normalize_card_type(card_type_input, group_size_input)
        group_size = (int(group_size_input)
                      if card_type == 'family' and group_size_input.isdigit()
                      else default_size)

        with get_db_session() as db:
            if db.query(Guest).filter_by(phone=phone).first():
                flash(f"Guest with phone {phone} already exists.", "warning")
                return redirect(url_for('add_guest'))

            visual_id = get_next_visual_id(db)
            qr_id     = f"GUEST-{visual_id:04d}"

            try:
                qr_bytes = generate_qr_bytes(qr_id)
                qr_fname = f"{qr_id}-{get_safe_filename_name_part(name or 'GUEST')}.png"
                qr_url   = upload_to_supabase(QR_BUCKET, qr_fname, qr_bytes,
                                              content_type="image/png")
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
            flash(f"Guest '{name or phone}' added. Card: {card_type.title()}, "
                  f"entries: {group_size}.", "success")
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
            if raw in ["s", "single"]:          return "single"
            if raw in ["d", "double"]:          return "double"
            if raw in ["f", "family", "group"]: return "family"
            return "single"

        with get_db_session() as db:
            for row in reader:
                name      = get_row(row, "name", "Name")
                raw_phone = get_row(row, "phone", "Phone")
                if not raw_phone:
                    skipped += 1
                    continue

                phone     = to_whatsapp_number(raw_phone)
                card_type = normalize(get_row(row, "Card Type", "card_type", "type"))

                if card_type == "single":
                    group_size = 1
                elif card_type == "double":
                    group_size = 2
                else:
                    try:
                        group_size = max(1, int(get_row(
                            row, "Allowed", "allowed", "Size", "size",
                            "Group Size", "group_size")))
                    except Exception:
                        group_size = 1

                if db.query(Guest).filter_by(phone=phone).first():
                    skipped += 1
                    continue

                visual_id = get_next_visual_id(db)
                qr_id     = f"GUEST-{visual_id:04d}"
                qr_fname  = f"{qr_id}-{get_safe_filename_name_part(name or 'GUEST')}.png"

                try:
                    qr_bytes = generate_qr_bytes(qr_id)
                    qr_url   = upload_to_supabase(QR_BUCKET, qr_fname, qr_bytes,
                                                  content_type="image/png")
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
    data       = request.get_json() or {}
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
                           "card_type": (guest.card_type or "").title(),
                           "remaining_entries": 0}
                )

            guest.checked_in_count = (guest.checked_in_count or 0) + 1
            if guest.checked_in_count >= guest.group_size:
                guest.has_entered = True
                guest.entry_time  = datetime.now()

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

# -------------------- search_guests --------------------

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
            "visual_id":   g.visual_id, "name": g.name, "phone": g.phone,
            "qr_code_url": g.qr_code_url, "has_entered": g.has_entered,
            "entry_time":  g.entry_time.strftime('%Y-%m-%d %H:%M:%S') if g.entry_time else 'N/A',
            "card_type":   g.card_type
        } for g in guests])

# -------------------- download_excel --------------------

@app.route('/download_excel')
@login_required
def download_excel():
    with get_db_session() as db:
        guests = db.query(Guest).all()

    def ct(g): return (g.card_type or "").strip().lower()

    total_guests         = len(guests)
    single_cards         = sum(1 for g in guests if ct(g) == "single")
    double_cards         = sum(1 for g in guests if ct(g) == "double")
    family_cards         = sum(1 for g in guests if ct(g) == "family")
    total_family_allowed = sum(g.group_size for g in guests if ct(g) == "family")
    entered_guests       = sum(1 for g in guests if bool(g.has_entered))

    wb = Workbook()
    ws = wb.active
    ws.title = "Guest Report"
    ws["A1"]      = "Guest Summary Report"
    ws["A1"].font = Font(size=14, bold=True)

    summary_data = [
        ("Total Guests", total_guests),
        ("Single Cards", single_cards),
        ("Double Cards", double_cards),
        ("Family Cards", family_cards),
        ("Total Allowed by Family Cards", total_family_allowed),
        ("Guests Entered", entered_guests),
        ("Guests Not Entered", total_guests - entered_guests),
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
        ws.cell(i, 1, g.id);        ws.cell(i, 2, g.name);       ws.cell(i, 3, g.phone)
        ws.cell(i, 4, g.qr_code_id)
        ws.cell(i, 5, "Entered" if g.has_entered else "Not Entered")
        ws.cell(i, 6, g.entry_time.strftime('%Y-%m-%d %H:%M:%S') if g.entry_time else "")
        ws.cell(i, 7, g.visual_id); ws.cell(i, 8, g.card_type);  ws.cell(i, 9, g.group_size)
        ws.cell(i, 10, "Yes" if g.has_whatsapp else ("No" if g.has_whatsapp is False else "Unknown"))
        ws.cell(i, 11, g.rsvp_status or "—")
        ws.cell(i, 12, "Yes" if g.at_sms_sent else "No")

    first_data_row = table_start + 1
    last_data_row  = table_start + len(guests)
    if last_data_row >= first_data_row:
        rng = f"E{first_data_row}:E{last_data_row}"
        ws.conditional_formatting.add(rng, CellIsRule(
            operator="equal", formula=['"Entered"'],
            fill=PatternFill(start_color="C6EFCE", end_color="C6EFCE", fill_type="solid")))
        ws.conditional_formatting.add(rng, CellIsRule(
            operator="equal", formula=['"Not Entered"'],
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
                data  = download_from_supabase(QR_BUCKET, fname)
                zf.writestr(fname, data)
            except Exception as e:
                current_app.logger.warning(f"Could not fetch QR for {guest.name}: {e}")

    memory_file.seek(0)
    return send_file(memory_file, download_name='qr_codes.zip',
                     as_attachment=True, mimetype='application/zip')

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
                guest.name  = request.form.get('name', guest.name).strip()
                guest.phone = to_whatsapp_number(request.form.get('phone', guest.phone))
                guest.has_entered = 'has_entered' in request.form

                new_card_type_raw = request.form.get('card_type', guest.card_type)
                group_size_raw    = request.form.get('group_size', '').strip()
                new_card_type, _  = normalize_card_type(new_card_type_raw, group_size_raw or None)

                if new_card_type == "family":
                    try:
                        new_group_size = max(1, int(request.form.get('group_size', '').strip()))
                    except Exception:
                        flash("Invalid group size for family card.", "danger")
                        return redirect(request.url)
                    if new_group_size < guest.checked_in_count:
                        flash(f"Group size cannot be less than checked-in count "
                              f"({guest.checked_in_count}).", "danger")
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

                guest.card_type  = new_card_type
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

            delete_from_supabase(QR_BUCKET,    qr_filename_from_guest(guest))
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
                qr_id    = f"GUEST-{guest.visual_id:04d}"
                qr_fname = qr_filename_from_guest(guest)
                qr_bytes = generate_qr_bytes(qr_id)
                qr_url   = upload_to_supabase(QR_BUCKET, qr_fname, qr_bytes,
                                              content_type="image/png")
                guest.qr_code_id  = qr_id
                guest.qr_code_url = qr_url
            db.commit()
            flash("QR codes regenerated.", "success")
        except Exception as e:
            db.rollback()
            flash(f"Error regenerating QR codes: {e}", "danger")
            current_app.logger.error(f"Error regenerating QR codes: {e}", exc_info=True)

    return redirect(url_for('view_all'))

# ---------------------------------------------------------------------------
# ── CARD GENERATION  (per-card API — avoids Gunicorn timeout) ───────────────
#
#  OLD approach: /generate_guest_cards looped over all guests in one request
#               → timed out after ~30 s on Render free tier.
#
#  NEW approach:
#    1. GET  /generate_guest_cards          → returns list of visual_ids (JSON)
#    2. POST /generate_card/<visual_id>     → generates & uploads ONE card (JSON)
#    3. The browser calls step 2 one-by-one with a small progress bar.
#
#  The old HTML route still exists as /generate_guest_cards_legacy for safety.
# ---------------------------------------------------------------------------

@app.route('/generate_guest_cards')
@login_required
def generate_guest_cards():
    """
    Returns JSON list of all guest visual_ids so the frontend can call
    /generate_card/<visual_id> for each one individually.
    """
    template_path = os.path.join("static", "Card Template.jpg")
    if not os.path.exists(template_path):
        return jsonify(success=False,
                       error="Card template not found at static/Card Template.jpg"), 500

    try:
        _bold_font_path()
    except FileNotFoundError as e:
        return jsonify(success=False, error=str(e)), 500

    with get_db_session() as db:
        guests = db.query(Guest).order_by(Guest.visual_id).all()
        ids    = [g.visual_id for g in guests if g.qr_code_url]

    return jsonify(success=True, visual_ids=ids, total=len(ids))


@app.route('/generate_card/<int:visual_id>', methods=['POST'])
@login_required
def generate_card(visual_id):
    """
    Generate and upload the invitation card for a single guest.
    Called by the frontend one guest at a time to avoid timeouts.
    Returns JSON {success, name, visual_id, error?}.
    """
    with get_db_session() as db:
        guest = db.query(Guest).filter_by(visual_id=visual_id).first()
        if not guest:
            return jsonify(success=False, visual_id=visual_id,
                           error="Guest not found"), 404

        if not guest.qr_code_url:
            return jsonify(success=False, visual_id=visual_id,
                           error="No QR code URL — regenerate QR codes first"), 400

        ok = _render_and_upload_card(guest)

    if ok:
        return jsonify(success=True, visual_id=visual_id, name=guest.name)
    else:
        return jsonify(success=False, visual_id=visual_id,
                       error="Card rendering failed — check server logs"), 500


@app.route('/generate_cards_page')
@login_required
def generate_cards_page():
    """
    Renders the progress-bar page that drives the per-card generation.
    Add a link to this route in your guests.html template.
    """
    return render_template('generate_cards.html')


# ── legacy single-request route (kept for reference, not recommended) ────────

@app.route('/generate_guest_cards_legacy')
@login_required
def generate_guest_cards_legacy():
    template_path = os.path.join("static", "Card Template.jpg")
    if not os.path.exists(template_path):
        flash("Card template not found at static/Card Template.jpg", "danger")
        return redirect(url_for('view_all'))

    try:
        _bold_font_path()
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
                _render_and_upload_card(guest)
            except Exception as e:
                flash(f"Failed card for {guest.name}: {e}", "danger")
                current_app.logger.error(
                    f"Card gen error for guest {guest.visual_id}: {e}", exc_info=True)

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
            img     = _draw_card(guest, qr_img)

            buf = BytesIO()
            img.save(buf, format="JPEG", quality=92)
            buf.seek(0)

            return send_file(
                buf,
                as_attachment=True,
                download_name=f"Guest-{guest.visual_id:04d}.jpg",
                mimetype="image/jpeg",
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
                data  = download_from_supabase(CARDS_BUCKET, fname)
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
            "total_guests":       total,
            "single_cards":       db.query(Guest).filter_by(card_type='single').count(),
            "double_cards":       db.query(Guest).filter_by(card_type='double').count(),
            "family_cards":       db.query(Guest).filter_by(card_type='family').count(),
            "entered_guests":     db.query(Guest).filter_by(has_entered=True).count(),
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
                delete_from_supabase(QR_BUCKET,    qr_filename_from_guest(guest))
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
        mode      = request.args.get('hub.mode')
        token     = request.args.get('hub.verify_token')
        challenge = request.args.get('hub.challenge')
        if mode == 'subscribe' and token == WHATSAPP_VERIFY_TOKEN:
            current_app.logger.info("Webhook verified by Meta.")
            return challenge, 200
        return "Forbidden", 403

    try:
        data    = request.get_json()
        current_app.logger.info(f"Webhook payload: {data}")
        entries = data.get('entry', [])
        for entry in entries:
            for change in entry.get('changes', []):
                value    = change.get('value', {})
                messages = value.get('messages', [])
                for msg in messages:
                    msg_type    = msg.get('type')
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
        current_app.logger.error(f"Webhook error: {e}", exc_info=True)

    return "OK", 200


def _handle_rsvp(from_number: str, button_text: str):
    """Update RSVP status when a guest taps a template button."""
    positive = {"nitakuwepo", "i'll be there", "attending", "yes"}
    negative = {"sitokuwepo", "can't make it", "not attending", "no"}
    text     = button_text.lower()

    if text in positive:
        status = "attending"
    elif text in negative:
        status = "not_attending"
    else:
        return

    with get_db_session() as db:
        guest = db.query(Guest).filter_by(phone=from_number).first()
        if guest:
            guest.rsvp_status = status
            db.commit()
            logging.info(f"RSVP updated: {from_number} → {status}")
        else:
            logging.warning(f"RSVP received from unknown number: {from_number}")

# -------------------- Bulk WhatsApp send --------------------

@app.route('/send_whatsapp_invites', methods=['POST'])
@login_required
def send_whatsapp_invites():
    """
    Send WhatsApp template invitations to all guests who haven't been sent one.
    Each card image is uploaded to Meta (via whatsapp.send_guest_card) then the
    template is dispatched with the guest's unique media_id, name, and card number.
    """
    results   = {"sent": 0, "failed": 0, "invalid": 0, "skipped": 0}
    errors    = []

    with get_db_session() as db:
        guests = db.query(Guest).all()

        for guest in guests:
            # Skip if already sent
            if getattr(guest, 'has_whatsapp', None) is True:
                results["skipped"] += 1
                continue

            if not guest.phone:
                results["failed"] += 1
                continue

            try:
                # Fetch the card image from Supabase
                card_fname = card_filename_from_guest(guest)
                card_bytes = download_from_supabase(CARDS_BUCKET, card_fname)
            except Exception as e:
                current_app.logger.warning(
                    f"Could not fetch card for {guest.name} — skipping: {e}")
                results["failed"] += 1
                errors.append(f"{guest.name}: card not found (generate cards first)")
                continue

            try:
                result = send_guest_card(
                    to=guest.phone,
                    guest_name=guest.name or "Guest",
                    visual_id=guest.visual_id,
                    card_type=guest.card_type or "single",
                    image_bytes=card_bytes,
                    filename=card_fname,
                )

                status = result.get("status")
                if status == "sent":
                    guest.has_whatsapp = True
                    results["sent"] += 1
                elif status == "invalid_number":
                    guest.has_whatsapp = False
                    results["invalid"] += 1
                else:
                    results["failed"] += 1

            except Exception as e:
                current_app.logger.error(
                    f"WhatsApp send error for {guest.name}: {e}", exc_info=True)
                results["failed"] += 1
                errors.append(f"{guest.name}: {e}")

            # Small delay to respect Meta rate limits
            time.sleep(0.5)

        db.commit()

    summary = (f"Sent: {results['sent']}, Invalid numbers: {results['invalid']}, "
               f"Failed: {results['failed']}, Skipped (already sent): {results['skipped']}")
    flash(summary, "info")

    if errors:
        flash("Errors: " + " | ".join(errors[:10]), "warning")

    return redirect(url_for('view_all'))


# -------------------- Single WhatsApp resend --------------------

@app.route('/resend_whatsapp/<int:visual_id>', methods=['POST'])
@login_required
def resend_whatsapp(visual_id):
    """Re-send WhatsApp invitation to a single guest (even if already sent)."""
    with get_db_session() as db:
        guest = db.query(Guest).filter_by(visual_id=visual_id).first()
        if not guest:
            flash("Guest not found.", "danger")
            return redirect(url_for('view_all'))

        try:
            card_fname = card_filename_from_guest(guest)
            card_bytes = download_from_supabase(CARDS_BUCKET, card_fname)
        except Exception as e:
            flash(f"Card not found for {guest.name}. Generate cards first.", "warning")
            return redirect(url_for('view_all'))

        try:
            result = send_guest_card(
                to=guest.phone,
                guest_name=guest.name or "Guest",
                visual_id=guest.visual_id,
                card_type=guest.card_type or "single",
                image_bytes=card_bytes,
                filename=card_fname,
            )
            status = result.get("status")
            if status == "sent":
                guest.has_whatsapp = True
                db.commit()
                flash(f"Invitation resent to {guest.name}.", "success")
            elif status == "invalid_number":
                guest.has_whatsapp = False
                db.commit()
                flash(f"{guest.name} does not have a WhatsApp account.", "warning")
            else:
                flash(f"Failed to resend to {guest.name}.", "danger")
        except Exception as e:
            flash(f"Error sending to {guest.name}: {e}", "danger")
            current_app.logger.error(f"Resend error for {visual_id}: {e}", exc_info=True)

    return redirect(url_for('view_all'))


# -------------------- AT SMS send --------------------

@app.route('/send_sms_invites', methods=['POST'])
@login_required
def send_sms_invites():
    if not at_configured():
        flash("Africa's Talking SMS is not configured.", "danger")
        return redirect(url_for('view_all'))

    sent = failed = skipped = 0
    with get_db_session() as db:
        guests = db.query(Guest).all()
        for guest in guests:
            if getattr(guest, 'at_sms_sent', False):
                skipped += 1
                continue
            if not guest.phone:
                failed += 1
                continue
            try:
                message = (f"Habari {guest.name or 'Mgeni'}! "
                           f"Umealikwa kwenye sherehe yetu. "
                           f"Namba yako ya kadi: {guest.qr_code_id}.")
                at_send_sms(guest.phone, message)
                guest.at_sms_sent = True
                sent += 1
            except Exception as e:
                current_app.logger.error(f"SMS error for {guest.name}: {e}")
                failed += 1
            time.sleep(0.2)
        db.commit()

    flash(f"SMS — Sent: {sent}, Failed: {failed}, Skipped: {skipped}", "info")
    return redirect(url_for('view_all'))