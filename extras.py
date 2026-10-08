# extras.py — Event mini-site + SMS reminders for the Wedding/Event system
#
# Plugged into app.py with ONE call at the bottom:  register_extras(app, ...)
# (see APPLY_THESE_CHANGES.md)

import os
import json
import time
import hmac
import hashlib
import logging
from io import BytesIO
from datetime import datetime, timezone, timedelta

from flask import (
    render_template, request, redirect, url_for, flash, jsonify, session
)
from PIL import Image, ImageOps

from models import Guest, Event, get_db_session

PUBLIC_BASE_URL = (os.getenv("PUBLIC_BASE_URL") or "").rstrip("/")
SITE_BUCKET     = os.getenv("SUPABASE_SITE_BUCKET", "event-sites")
SITE_MAX_GALLERY = 12

EVENT_TYPE_LABELS = {
    "Wedding":      "HARUSI",
    "Send-Off":     "SEND-OFF",
    "Birthday":     "SIKUKUU YA KUZALIWA",
    "Conference":   "MKUTANO",
    "Confirmation": "IBADA YA KIPAIMARA",
    "Corporate":    "SHUGHULI YA KAMPUNI",
    "Other":        "SHUGHULI",
}

SITE_THEMES = ("emerald", "burgundy", "navy", "rose")
REMINDER_KINDS = ("event", "michango")
REMINDER_COOLDOWN_HOURS = 6   # don't re-remind the same guest within this window


# ---------------------------------------------------------------------------
# Pure helpers (importable from app.py)
# ---------------------------------------------------------------------------

def _site_gallery(ev) -> list:
    try:
        data = json.loads(getattr(ev, "site_gallery", None) or "[]")
        return [u for u in data if isinstance(u, str)]
    except Exception:
        return []


def _guest_sig(qr_code_id: str) -> str:
    """Short signature so a guest link can't be guessed/enumerated."""
    key = (os.getenv("SECRET_KEY") or "swiftinvite").encode()
    return hmac.new(key, (qr_code_id or "").encode(), hashlib.sha256).hexdigest()[:10]


def event_site_path(event, guest=None) -> str:
    """'<slug>' or '<slug>?g=<qr_id>&s=<sig>' — also used as the WhatsApp URL-button variable."""
    slug = (getattr(event, "slug", "") or "") if event else ""
    qr = getattr(guest, "qr_code_id", None) if guest is not None else None
    if slug and qr:
        return f"{slug}?g={qr}&s={_guest_sig(qr)}"
    return slug


def event_site_url(event, guest=None) -> str:
    """Public mini-site URL for an event (personalised when a guest is given),
    or '' if the site is off / not configurable. Never raises."""
    try:
        if not event or not getattr(event, "site_enabled", False) or not event.slug:
            return ""
        base = PUBLIC_BASE_URL
        if not base:
            try:
                base = request.host_url.rstrip("/")
            except RuntimeError:
                return ""
        return f"{base}/e/{event_site_path(event, guest)}"
    except Exception:
        return ""


def _ev_fields(event):
    weds  = (getattr(event, "weds_names", None)  or os.getenv("EVENT_WEDS_NAMES", "the Bride & Groom")) if event else os.getenv("EVENT_WEDS_NAMES", "the Bride & Groom")
    day   = (getattr(event, "event_day", None)   or os.getenv("EVENT_DAY", "")) if event else os.getenv("EVENT_DAY", "")
    date  = (getattr(event, "event_date", None)  or os.getenv("EVENT_DATE", "")) if event else os.getenv("EVENT_DATE", "")
    venue = (getattr(event, "event_venue", None) or os.getenv("EVENT_VENUE", "")) if event else os.getenv("EVENT_VENUE", "")
    etype = (getattr(event, "event_type", None)  or "Wedding") if event else "Wedding"
    etime = (getattr(event, "site_event_time", None) or "12:00 Jioni") if event else "12:00 Jioni"
    return weds, day, date, venue, etype, etime


def build_reminder_sms(guest, event=None, kind="event", when_text="") -> str:
    weds, day, date, venue, etype, etime = _ev_fields(event)
    label    = EVENT_TYPE_LABELS.get(etype, etype.upper())
    site_url = event_site_url(event, guest)
    when_text = (when_text or "").strip()

    if kind == "michango":
        info = (getattr(event, "contribution_info", None) or "").strip() if event else ""
        parts = [
            "KUMBUSHO LA MICHANGO",
            f"Habari {guest.name},",
            "",
            f"Tunakukumbusha kwa upole kuhusu mchango/ahadi yako kwa ajili ya "
            f"{label} ya {weds.upper()} itakayofanyika {day.upper()}, {date.upper()}.",
        ]
        if info:
            parts += ["", "Namna ya kuchangia:", info]
        if site_url:
            parts += ["", f"Maelezo zaidi: {site_url}"]
        parts += ["", "Asante sana kwa ushirikiano wako. Mungu akubariki."]
        return "\n".join(parts)

    head = f"KUMBUSHO - {when_text.upper()}" if when_text else "KUMBUSHO"
    parts = [
        head,
        f"Habari {guest.name},",
        f"Tunakukumbusha kuhusu {label} ya {weds.upper()}:",
        f"{day.upper()}, {date.upper()}",
        f"Saa {etime}",
        venue.upper(),
        "",
        f"Namba ya Kadi: {str(guest.visual_id or 0).zfill(4)} - {(guest.card_type or 'Single').title()}",
        "Usisahau kuja na kadi yako.",
    ]
    if site_url:
        parts += [f"Maelezo & picha: {site_url}"]
    parts += ["Karibu sana!"]
    return "\n".join(parts)


def _clean(v):
    return (v or "").strip()


def _upload_site_image(ev, file_storage, tag, upload_fn, max_px=1600) -> str:
    img = Image.open(BytesIO(file_storage.read()))
    img = ImageOps.exif_transpose(img).convert("RGB")
    img.thumbnail((max_px, max_px), Image.LANCZOS)
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=85, optimize=True)
    fname = f"{ev.slug}/{tag}-{int(time.time() * 1000)}.jpg"
    return upload_fn(SITE_BUCKET, fname, buf.getvalue(), content_type="image/jpeg")


def _hours_since(dt):
    if not dt:
        return None
    if dt.tzinfo is None:
        # The app stores now_eat() into naive DateTime columns, so a naive
        # value read back is East Africa Time wall-clock, not UTC.
        dt = dt.replace(tzinfo=timezone(timedelta(hours=3)))
    return (datetime.now(timezone.utc) - dt).total_seconds() / 3600.0


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------

def register_extras(app, *, get_active_event, now_eat, to_whatsapp_number,
                    upload_to_supabase, delete_from_supabase,
                    login_required, admin_required,
                    at_send_sms, at_configured):

    # ── Public mini-site (no login) ─────────────────────────────────────
    @app.route("/e/<slug>")
    def public_event_site(slug):
        with get_db_session() as db:
            ev = db.query(Event).filter_by(slug=slug).first()
            if not ev or not getattr(ev, "site_enabled", False):
                return ("<h2 style='font-family:sans-serif;text-align:center;margin-top:20vh'>"
                        "Ukurasa haupatikani.</h2>"), 404
            weds, day, date, venue, etype, etime = _ev_fields(ev)
            gname = ""
            _g, _s = request.args.get("g", ""), request.args.get("s", "")
            if _g and _s and hmac.compare_digest(_s, _guest_sig(_g)):
                _guest = db.query(Guest).filter_by(qr_code_id=_g, event_id=ev.id).first()
                if _guest and _guest.name:
                    gname = _guest.name.strip()
            data = {
                "guest":     gname,
                "name":      ev.name,
                "label":     EVENT_TYPE_LABELS.get(etype, etype.upper()),
                "weds":      weds,
                "day":       day,
                "date":      date,
                "venue":     venue,
                "time":      etime,
                "story":     getattr(ev, "site_story", None) or "",
                "hero":      getattr(ev, "site_hero_url", None) or "",
                "gallery":   _site_gallery(ev),
                "map_url":   getattr(ev, "site_map_url", None) or "",
                "contact":   getattr(ev, "site_contact", None) or "",
                "dress":     getattr(ev, "site_dress_code", None) or "",
                "contribute": getattr(ev, "contribution_info", None) or "",
                "theme":     (getattr(ev, "site_theme", None) if getattr(ev, "site_theme", None) in SITE_THEMES else "emerald"),
            }
        resp = app.make_response(render_template("event_site.html", e=data))
        resp.headers["Cache-Control"] = ("private, max-age=60" if data["guest"] else "public, max-age=60")
        return resp

    # ── Admin: edit mini-site ───────────────────────────────────────────
    @app.route("/events/<int:event_id>/site", methods=["GET", "POST"])
    @admin_required
    def event_site_edit(event_id):
        with get_db_session() as db:
            ev = db.get(Event, event_id)
            if not ev:
                flash("Event not found.", "danger")
                return redirect(url_for("events_list"))

            if request.method == "POST":
                ev.site_enabled      = "site_enabled" in request.form
                ev.site_event_time   = _clean(request.form.get("site_event_time")) or None
                ev.site_story        = _clean(request.form.get("site_story")) or None
                ev.site_contact      = _clean(request.form.get("site_contact")) or None
                ev.site_dress_code   = _clean(request.form.get("site_dress_code")) or None
                ev.contribution_info = _clean(request.form.get("contribution_info")) or None
                theme = _clean(request.form.get("site_theme"))
                ev.site_theme = theme if theme in SITE_THEMES else "emerald"

                map_url = _clean(request.form.get("site_map_url"))
                if map_url and not map_url.lower().startswith(("http://", "https://")):
                    flash("Map link must start with http:// or https:// — it was not saved.", "warning")
                    map_url = getattr(ev, "site_map_url", None) or ""
                ev.site_map_url = map_url or None

                try:
                    hero = request.files.get("hero")
                    if hero and hero.filename:
                        ev.site_hero_url = _upload_site_image(ev, hero, "hero", upload_to_supabase, 1800)

                    gallery = _site_gallery(ev)
                    added = 0
                    for i, f in enumerate(request.files.getlist("images")):
                        if not f or not f.filename:
                            continue
                        if len(gallery) >= SITE_MAX_GALLERY:
                            flash(f"Gallery limit is {SITE_MAX_GALLERY} photos; extra files ignored.", "warning")
                            break
                        gallery.append(_upload_site_image(ev, f, f"g{i}", upload_to_supabase))
                        added += 1
                    ev.site_gallery = json.dumps(gallery)
                except Exception as e:
                    msg = str(e)
                    if "Bucket not found" in msg or "404" in msg:
                        flash(f'Storage bucket "{SITE_BUCKET}" not found. Create it in Supabase '
                              f'(Storage → New bucket, set to Public).', "danger")
                    else:
                        flash(f"Image upload failed: {msg}", "danger")
                    logging.exception("site image upload failed")

                db.commit()
                flash("Event page saved.", "success")
                return redirect(url_for("event_site_edit", event_id=event_id))

            data = {
                "id": ev.id, "name": ev.name, "slug": ev.slug,
                "site_enabled":      bool(getattr(ev, "site_enabled", False)),
                "site_event_time":   getattr(ev, "site_event_time", None) or "",
                "site_story":        getattr(ev, "site_story", None) or "",
                "site_contact":      getattr(ev, "site_contact", None) or "",
                "site_dress_code":   getattr(ev, "site_dress_code", None) or "",
                "site_map_url":      getattr(ev, "site_map_url", None) or "",
                "contribution_info": getattr(ev, "contribution_info", None) or "",
                "site_theme":        getattr(ev, "site_theme", None) or "emerald",
                "hero":              getattr(ev, "site_hero_url", None) or "",
                "gallery":           _site_gallery(ev),
            }
            public_url = (f"{PUBLIC_BASE_URL}/e/{ev.slug}" if PUBLIC_BASE_URL
                          else f"{request.host_url.rstrip('/')}/e/{ev.slug}")
        return render_template("event_site_form.html", event=data, public_url=public_url)

    @app.route("/events/<int:event_id>/site/remove_image", methods=["POST"])
    @admin_required
    def event_site_remove_image(event_id):
        url = _clean(request.form.get("url"))
        with get_db_session() as db:
            ev = db.get(Event, event_id)
            if not ev:
                flash("Event not found.", "danger")
                return redirect(url_for("events_list"))
            path = url.split(f"/{SITE_BUCKET}/", 1)[-1].split("?")[0] if url else ""
            if url and url == (getattr(ev, "site_hero_url", None) or ""):
                ev.site_hero_url = None
                if path: delete_from_supabase(SITE_BUCKET, path)
            else:
                gallery = _site_gallery(ev)
                if url in gallery:
                    gallery.remove(url)
                    ev.site_gallery = json.dumps(gallery)
                    if path: delete_from_supabase(SITE_BUCKET, path)
            db.commit()
        flash("Photo removed.", "success")
        return redirect(url_for("event_site_edit", event_id=event_id))

    # ── Reminders ───────────────────────────────────────────────────────
    class _Sample:
        name = "Mgeni Mfano"; visual_id = 12; card_type = "single"

    @app.route("/reminders")
    @admin_required
    def reminders_page():
        with get_db_session() as db:
            ev = get_active_event(db)
            guests = db.query(Guest).filter_by(event_id=ev.id if ev else None).all()
            ctx = dict(
                event_name=ev.name if ev else "No Event",
                total=len(guests),
                with_phone=sum(1 for g in guests if g.phone),
                declined=sum(1 for g in guests if g.rsvp_status == "not_attending"),
                entered=sum(1 for g in guests if (g.checked_in_count or 0) >= 1),
                sms_enabled=at_configured(),
                site_url=event_site_url(ev),
            )
        return render_template("reminders.html", **ctx)

    @app.route("/reminder_preview", methods=["POST"])
    @admin_required
    def reminder_preview():
        d = request.get_json() or {}
        kind = d.get("kind", "event")
        if kind not in REMINDER_KINDS:
            return jsonify(success=False, error="Unknown reminder type"), 400
        with get_db_session() as db:
            ev = get_active_event(db)
            text = build_reminder_sms(_Sample(), ev, kind, d.get("when_text", ""))
        return jsonify(success=True, text=text, length=len(text),
                       segments=max(1, -(-len(text) // 153)) if len(text) > 160 else 1)

    @app.route("/reminder_ids", methods=["POST"])
    @admin_required
    def reminder_ids():
        d = request.get_json() or {}
        kind         = d.get("kind", "event")
        force        = bool(d.get("force"))
        skip_declined = bool(d.get("skip_declined", kind == "event"))
        skip_entered  = bool(d.get("skip_entered", False))
        if kind not in REMINDER_KINDS:
            return jsonify(success=False, error="Unknown reminder type"), 400
        ids = []
        with get_db_session() as db:
            ev = get_active_event(db)
            for g in db.query(Guest).filter_by(event_id=ev.id if ev else None)\
                       .order_by(Guest.visual_id).all():
                if not g.phone:
                    continue
                if skip_declined and g.rsvp_status == "not_attending":
                    continue
                if skip_entered and (g.checked_in_count or 0) >= 1:
                    continue
                if not force:
                    age = _hours_since(getattr(g, "reminder_sms_sent_at", None))
                    if age is not None and age < REMINDER_COOLDOWN_HOURS:
                        continue
                ids.append(g.id)
        return jsonify(success=True, guest_ids=ids, total=len(ids))

    @app.route("/send_reminder_one/<int:guest_id>", methods=["POST"])
    @admin_required
    def send_reminder_one(guest_id):
        if not at_configured():
            return jsonify(success=False, guest_id=guest_id,
                           error="Africa's Talking not configured."), 400
        d = request.get_json() or {}
        kind = d.get("kind", "event")
        if kind not in REMINDER_KINDS:
            return jsonify(success=False, guest_id=guest_id, error="Unknown reminder type"), 400
        try:
            with get_db_session() as db:
                guest = db.get(Guest, guest_id)
                if not guest:
                    return jsonify(success=False, guest_id=guest_id, error="Guest not found"), 404
                ev = db.get(Event, guest.event_id) if guest.event_id else get_active_event(db)
                phone = to_whatsapp_number(guest.phone)
                msg = build_reminder_sms(guest, ev, kind, d.get("when_text", ""))
                result = at_send_sms(phone, msg)
                if result.get("success"):
                    guest.reminder_sms_sent_at = now_eat()
                    guest.reminder_sms_count   = (getattr(guest, "reminder_sms_count", 0) or 0) + 1
                    guest.reminder_sms_error   = None
                    db.commit()
                    return jsonify(success=True, guest_id=guest_id, name=guest.name)
                err = str(result.get("error", "SMS failed"))[:500]
                guest.reminder_sms_error = err
                db.commit()
                return jsonify(success=False, guest_id=guest_id, name=guest.name, error=err)
        except Exception as e:
            logging.exception(f"send_reminder_one failed for {guest_id}")
            return jsonify(success=False, guest_id=guest_id, error=str(e)), 500