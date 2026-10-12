# extras.py — Event mini-site + SMS reminders for the Wedding/Event system
#
# Plugged into app.py with ONE call at the bottom:  register_extras(app, ...)
# (see APPLY_THESE_CHANGES.md)

import os
import csv
import json
import time
import hmac
import hashlib
import logging
from io import BytesIO, StringIO
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

SITE_THEMES = ("emerald", "burgundy", "navy", "rose", "bwg")
REMINDER_KINDS = ("event", "michango", "personal")
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

    if kind == "personal":
        pledge = int(getattr(guest, "pledge_amount", 0) or 0)
        paid   = int(getattr(guest, "paid_amount", 0) or 0)
        info   = (getattr(event, "contribution_info", None) or "").strip() if event else ""
        lines = ["KUMBUSHO LA AHADI", f"Habari {guest.name},", "",
                 f"Tunakukumbusha kwa upendo kuhusu ahadi yako ya TSh {pledge:,} kwa ajili ya {label} ya {weds.upper()}."]
        if paid > 0:
            lines.append(f"Umeshalipa: TSh {paid:,}")
        lines.append(f"Salio: TSh {max(pledge - paid, 0):,}")
        if info:
            lines += ["", "Namna ya kulipa:", info]
        if site_url:
            lines += ["", f"Maelezo: {site_url}"]
        lines += ["", "Tafadhali toa taarifa baada ya kulipa. Asante sana, Mungu akubariki."]
        return "\n".join(lines)

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


# ---------------------------------------------------------------------------
# Black, White & Gold wedding website (public, read-only)
# ---------------------------------------------------------------------------
import re as _re
from urllib.parse import quote as _q

_EAT = timezone(timedelta(hours=3))
_SW_D = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
_SW_M = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]
_DEFAULT_PALETTE = [("Dusty Rose", "#b77e78"), ("Sage Green", "#9ba58d"), ("Champagne", "#d9c3a5"), ("Ivory", "#f7f0e5")]

# (key, label, kind)  kind: text | dt | date | area
WED_FIELDS = [
    ("bride", "Bride's name", "text"), ("groom", "Groom's name", "text"),
    ("initials", "Monogram initials (max 3 letters)", "text"), ("tagline", "Short tagline", "text"),
    ("city", "City / location", "text"), ("hashtag", "Wedding hashtag (e.g. #AsaAndJuma)", "text"),
    ("wedding_dt", "Wedding date & time (East Africa Time) — enables countdown and Save the Date", "dt"),
    ("cer_name", "Ceremony venue name", "text"), ("cer_addr", "Ceremony full address", "text"),
    ("cer_dt", "Ceremony date & time", "dt"), ("cer_maps", "Ceremony Google Maps link (optional; auto-built from address if empty)", "text"),
    ("rec_name", "Reception venue name", "text"), ("rec_addr", "Reception full address", "text"),
    ("rec_dt", "Reception date & time", "dt"), ("rec_maps", "Reception Google Maps link (optional)", "text"),
    ("dress_note", "Attire note (dress code itself comes from the Event Page)", "area"),
    ("rsvp_deadline", "RSVP deadline", "date"),
    ("wa", "WhatsApp number for RSVP — international digits only, e.g. 255712345678", "text"),
    ("email", "Contact email", "text"), ("instagram", "Instagram URL (optional)", "text"),
    ("portrait", "Couple portrait image URL (optional; defaults to first gallery photo)", "text"),
    ("palette", "Theme colours, one per line:  Name #HEX   (e.g. Champagne Gold #C9A45C)", "area"),
    ("faq", "FAQ, one per line:  Question | Answer", "area"),
    ("embeds", "Embedded map URLs (optional, Google Maps 'Embed a map' src links, one per line)", "area"),
    ("story_title", "Story heading (default: Our story is bringing us to you.)", "text"),
    ("schedule", "Order of the day, one per line:  9:30 AM | Guest arrival | Short description (optional)", "area"),
]


def _wdt(iso):
    try:
        return datetime.strptime((iso or "").strip()[:16], "%Y-%m-%dT%H:%M").replace(tzinfo=_EAT)
    except Exception:
        return None


def _wfmt(d):
    """-> (long date, 12h time, SHORT UPPERCASE date)"""
    if not d:
        return ("", "", "")
    long_ = f"{_SW_D[d.weekday()]}, {d.day} {_SW_M[d.month - 1]} {d.year}"
    return (long_, f"{d.hour % 12 or 12}:{d.minute:02d} {'AM' if d.hour < 12 else 'PM'}",
            f"{_SW_D[d.weekday()][:3]}, {d.day} {_SW_M[d.month - 1]} {d.year}".upper())


def _safe_url(u):
    u = (u or "").strip()
    return u if u.lower().startswith(("http://", "https://")) and " " not in u else ""


def _wextra(ev):
    try:
        d = json.loads(getattr(ev, "site_extra", None) or "{}")
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _map_link(url, addr):
    return _safe_url(url) or (f"https://www.google.com/maps/dir/?api=1&destination={_q(addr)}" if addr else "")


def build_wedding(ev, guest_name="", dev=False):
    """Public-only view model. Never includes guests, phones (other than the configured RSVP
    number) or admin data."""
    x = _wextra(ev)
    g = lambda k: (x.get(k) or "").strip()
    bride, groom = g("bride"), g("groom")
    base = (getattr(ev, "weds_names", None) or ev.name or "").strip()
    names = f"{bride} & {groom}" if bride and groom else (base or "Bride & Groom")
    initials = _re.sub(r"[^A-Za-z0-9&]", "", g("initials") or "".join(p[0] for p in names.replace("&", " ").split()[:2]).upper())[:3] or "W"
    gallery = _site_gallery(ev)
    hero = getattr(ev, "site_hero_url", None) or ""
    wd = _wdt(g("wedding_dt")) or _wdt(g("cer_dt"))
    legacy = ", ".join(p for p in [getattr(ev, "event_day", None), getattr(ev, "event_date", None)] if p)
    date_str = _wfmt(wd)[0] or legacy or "Date to be confirmed"

    def block(p, fallback_name=""):
        d = _wdt(g(p + "_dt")) or (wd if p == "cer" else None)
        ds, ts, _sh = _wfmt(d)
        b = dict(name=g(p + "_name") or fallback_name, addr=g(p + "_addr"), date=ds, time=ts,
                 maps=_map_link(g(p + "_maps"), g(p + "_addr")))
        if p == "cer":
            b["date"] = ds or legacy
            b["time"] = ts or (getattr(ev, "site_event_time", None) or "")
        return b if (b["name"] or b["addr"] or b["date"]) else None

    cer = block("cer", getattr(ev, "event_venue", None) or "")
    rec = block("rec")
    palette = []
    for line in g("palette").splitlines():
        m = _re.match(r"^(.*?)\s*(#[0-9a-fA-F]{6})$", line.strip())
        if m:
            palette.append((m.group(1).strip() or m.group(2), m.group(2)))
    faq = [tuple(p.strip() for p in ln.split("|", 1)) for ln in g("faq").splitlines() if "|" in ln]
    wa = _re.sub(r"\D", "", g("wa"))
    msg = (f"Hello! My name is {guest_name or '[your name]'}. I would love to confirm my attendance at the wedding of "
           f"{names}. Number of guests: [number]. Thank you!")
    parts = [bride, groom] if bride and groom else [p.strip() for p in _re.split(r"\s*(?:&|\band\b|\bna\b)\s*", base) if p.strip()]
    if len(parts) != 2:
        parts = [names]
    ph = getattr(ev, "site_contact", None) or ""
    return dict(
        slug=ev.slug, names=names, initials=initials, tagline=g("tagline") or "A celebration of love, laughter & forever",
        place=g("city") or (getattr(ev, "event_venue", None) or ""), hashtag=g("hashtag"),
        date_str=date_str, iso=wd.isoformat() if wd else "", has_date=bool(wd),
        hero=hero, portrait=_safe_url(g("portrait")) or (gallery[0] if gallery else hero),
        story=getattr(ev, "site_story", None) or "Some love stories feel like home from the very beginning. Ours has been filled with little moments, big dreams, laughter, friendship, and a love that keeps growing.\n\nNow, we are so excited to begin our next chapter as one, and even more excited to celebrate this special day with the people who have made our journey so meaningful.\n\nHaving you with us would make our day unforgettable.", gallery=gallery, cer=cer, rec=rec,
        dress=getattr(ev, "site_dress_code", None) or "", dress_note=g("dress_note"),
        deadline=g("rsvp_deadline"), palette=palette or _DEFAULT_PALETTE,
        faq=faq, embeds=[u for u in g("embeds").splitlines() if u.strip().startswith("https://www.google.com/maps/embed")],
        wa=wa, wa_href=(f"https://wa.me/{wa}?text={_q(msg)}" if wa else ""),
        email=g("email") if "@" in g("email") else "", instagram=_safe_url(g("instagram")),
        contact_text=ph, guest=guest_name, dev=dev, sample=not x,
        parts=parts, date_short=_wfmt(wd)[2] or (legacy.upper() if legacy else "DATE TO BE CONFIRMED"), date_long=date_str,
        hero_note=g("quote_hero") or "Love Brings Us Here", cap=g("polaroid_cap") or "Better Together",
        story_title=g("story_title") or "Our story is bringing us to you.",
        schedule=[tuple(p.strip() for p in (ln.split("|") + ["", ""])[:3]) for ln in g("schedule").splitlines() if "|" in ln],
        cer_img=_safe_url(g("cer_img")) or (gallery[1] if len(gallery) > 1 else ""),
        theme_img=_safe_url(g("theme_img")) or (gallery[2] if len(gallery) > 2 else ""),
        theme_words=g("theme_words") or "Classic · Modern · Timeless",
        theme_text=g("theme_text") or "Our wedding will be a beautiful blend of classic elegance and modern touches, with a touch of natural beauty.",
        colours_label=g("colours_label") or "Black · White · Gold", colours_quote=g("colours_quote") or "Simple colours, endless elegance.",
        closing=g("closing_line") or "Together is our favourite place to be",
        socials=[(n, u) for n, u in [("Facebook", _safe_url(g("facebook"))), ("Instagram", _safe_url(g("instagram"))), ("TikTok", _safe_url(g("tiktok"))), ("YouTube", _safe_url(g("youtube")))] if u],
    )


def _ics_esc(s):
    return (s or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def build_ics(title, start, location="", url=""):
    f = lambda d: d.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return "\r\n".join(["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//SwiftInvite//Wedding//EN", "CALSCALE:GREGORIAN",
        "BEGIN:VEVENT", f"UID:{f(start)}-{abs(hash(title)) % 10**8}@swiftinvite", f"DTSTAMP:{f(datetime.now(timezone.utc))}",
        f"DTSTART:{f(start)}", f"DTEND:{f(start + timedelta(hours=5))}", f"SUMMARY:{_ics_esc(title)}",
        f"LOCATION:{_ics_esc(location)}", f"URL:{url}" if url else "X-NOURL:1", "END:VEVENT", "END:VCALENDAR", ""])


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
            if getattr(ev, "site_theme", None) == "bwg":
                return _render_wedding(ev)
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
        name = "Mgeni Mfano"; visual_id = 12; card_type = "single"; pledge_amount = 100000; paid_amount = 40000

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
                owing=sum(1 for g in guests if (g.pledge_amount or 0) > (g.paid_amount or 0)),
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
                if kind == "personal" and (g.pledge_amount or 0) <= (g.paid_amount or 0):
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
                if kind == "personal" and (guest.pledge_amount or 0) <= (guest.paid_amount or 0):
                    return jsonify(success=False, guest_id=guest_id, name=guest.name, error="No outstanding balance")
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

    # ── Contributions (michango / ahadi) ────────────────────────────────
    def _num(v):
        try:
            return max(0, int(float(str(v).replace(",", "").replace(" ", "") or 0)))
        except Exception:
            return 0

    @app.route("/pledges")
    @admin_required
    def pledges_page():
        with get_db_session() as db:
            ev = get_active_event(db)
            gs = db.query(Guest).filter_by(event_id=ev.id if ev else None).order_by(Guest.visual_id).all()
            rows = [dict(id=g.id, vid=str(g.visual_id or 0).zfill(4), name=g.name or "", phone=g.phone or "",
                         pledge=g.pledge_amount or 0, paid=g.paid_amount or 0) for g in gs]
            name = ev.name if ev else "No Event"
        return render_template("pledges.html", rows=rows, event_name=name,
                               tot_pledge=sum(r["pledge"] for r in rows), tot_paid=sum(r["paid"] for r in rows))

    @app.route("/pledges/save/<int:gid>", methods=["POST"])
    @admin_required
    def pledge_save(gid):
        d = request.get_json() or {}
        with get_db_session() as db:
            g = db.get(Guest, gid)
            if not g:
                return jsonify(success=False, error="Guest not found"), 404
            if "pledge" in d: g.pledge_amount = _num(d["pledge"])
            if "paid" in d:   g.paid_amount   = _num(d["paid"])
            if "add" in d:    g.paid_amount   = max(0, (g.paid_amount or 0) + int(float(d["add"] or 0)))
            db.commit()
            return jsonify(success=True, pledge=g.pledge_amount or 0, paid=g.paid_amount or 0)

    @app.route("/pledges/import", methods=["POST"])
    @admin_required
    def pledges_import():
        f = request.files.get("file")
        if not f or not f.filename:
            flash("Choose a CSV file.", "danger")
            return redirect(url_for("pledges_page"))
        done = missed = 0
        reader = csv.DictReader(StringIO(f.read().decode("utf-8-sig")))
        with get_db_session() as db:
            ev = get_active_event(db)
            by_phone = {to_whatsapp_number(g.phone): g for g in
                        db.query(Guest).filter_by(event_id=ev.id if ev else None).all() if g.phone}
            for raw in reader:
                r = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
                g = by_phone.get(to_whatsapp_number(r.get("phone") or r.get("simu") or ""))
                if not g:
                    missed += 1
                    continue
                p = r.get("pledge") or r.get("ahadi")
                a = r.get("paid") or r.get("amelipa")
                if p not in (None, ""): g.pledge_amount = _num(p)
                if a not in (None, ""): g.paid_amount   = _num(a)
                done += 1
            db.commit()
        flash(f"Imported: {done} updated, {missed} phone numbers not found in this event.", "success")
        return redirect(url_for("pledges_page"))

    # ── Black, White & Gold public site: /w/<slug> ──────────────────────
    def _render_wedding(ev):
        gname = ""
        _g, _s = request.args.get("g", ""), request.args.get("s", "")
        if _g and _s and hmac.compare_digest(_s, _guest_sig(_g)):
            with get_db_session() as db2:
                _guest = db2.query(Guest).filter_by(qr_code_id=_g, event_id=ev.id).first()
                gname = (_guest.name or "").strip() if _guest else ""
        dev = bool(app.debug or os.getenv("FLASK_ENV") == "development")
        w = build_wedding(ev, gname, dev)
        base = PUBLIC_BASE_URL or request.host_url.rstrip("/")
        w["url"] = f"{base}/w/{ev.slug}"
        resp = app.make_response(render_template("wedding_bwg.html", w=w))
        resp.headers["Cache-Control"] = "private, max-age=60" if gname else "public, max-age=60"
        return resp

    @app.route("/w/<slug>")
    def public_wedding(slug):
        with get_db_session() as db:
            ev = db.query(Event).filter_by(slug=slug).first()
            if not ev or not getattr(ev, "site_enabled", False):
                return ("<h2 style='font-family:sans-serif;text-align:center;margin-top:20vh'>Page not found.</h2>"), 404
            return _render_wedding(ev)

    @app.route("/w/<slug>/calendar.ics")
    def public_wedding_ics(slug):
        with get_db_session() as db:
            ev = db.query(Event).filter_by(slug=slug).first()
            if not ev or not getattr(ev, "site_enabled", False):
                return "Not found", 404
            w = build_wedding(ev)
            start = _wdt(w["iso"][:16]) if w["iso"] else None
            if not start:
                return "Wedding date not configured", 404
            loc = (w["cer"] or {}).get("addr") or w["place"]
            body = build_ics(f"Wedding of {w['names']}", start, loc, (PUBLIC_BASE_URL or request.host_url.rstrip("/")) + f"/w/{slug}")
        return app.response_class(body, mimetype="text/calendar",
                                  headers={"Content-Disposition": f"attachment; filename=wedding-{slug}.ics"})

    @app.route("/events/<int:event_id>/site/wedding", methods=["GET", "POST"])
    @admin_required
    def event_wedding_edit(event_id):
        with get_db_session() as db:
            ev = db.get(Event, event_id)
            if not ev:
                flash("Event not found.", "danger")
                return redirect(url_for("events_list"))
            if request.method == "POST":
                ev.site_extra = json.dumps({k: (request.form.get(k) or "").strip()[:2000] for k, _, _ in WED_FIELDS})
                db.commit()
                flash("Wedding website details saved.", "success")
                return redirect(url_for("event_wedding_edit", event_id=event_id))
            data = dict(id=ev.id, name=ev.name, slug=ev.slug, enabled=bool(getattr(ev, "site_enabled", False)),
                        bwg=getattr(ev, "site_theme", None) == "bwg")
            x = _wextra(ev)
        return render_template("event_wedding_form.html", event=data, fields=WED_FIELDS, x=x)