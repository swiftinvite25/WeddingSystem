# extras.py — Event mini-site + SMS reminders for the Wedding/Event system
#
# Plugged into app.py with ONE call at the bottom:  register_extras(app, ...)
# (see APPLY_THESE_CHANGES.md)

import os
import types
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


def _sig_ok(qr_code_id, sig) -> bool:
    try:
        return bool(qr_code_id and sig) and hmac.compare_digest(str(sig).encode(), _guest_sig(qr_code_id).encode())
    except Exception:
        return False


def card_status(guest, event):
    """-> (cleared, reason, base). A card is released when the pledge is fully paid OR the minimum for the
    card type is reached (or the organiser overrides). With the gate OFF everybody is cleared (old behaviour)."""
    if not event or not getattr(event, "contrib_gate", False):
        return True, "gate off", 0
    ct = (getattr(guest, "card_type", None) or "single").lower()
    base = int({"single": getattr(event, "min_single", 0), "double": getattr(event, "min_double", 0)}.get(ct, getattr(event, "min_group", 0)) or 0)
    if getattr(guest, "card_override", False):
        return True, "override", base
    pledge, paid = int(getattr(guest, "pledge_amount", 0) or 0), int(getattr(guest, "paid_amount", 0) or 0)
    if pledge > 0 and paid >= pledge:
        return True, "pledge completed", base
    if base > 0 and paid >= base:
        return True, "minimum reached", base
    return False, "waiting", base


def card_cleared(guest, event) -> bool:
    return card_status(guest, event)[0]


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
    label = EVENT_TYPE_LABELS.get(etype, etype.upper())
    when_text = (when_text or "").strip()

    if kind == "personal":
        pledge = int(getattr(guest, "pledge_amount", 0) or 0)
        paid = int(getattr(guest, "paid_amount", 0) or 0)
        info = (getattr(event, "contribution_info", None) or "").strip() if event else ""

        lines = [
            f"KUMBUSHO LA AHADI: Habari {guest.name},",
            f"Tunakukumbushakwa upendo ahadi yako ya TSh {pledge:,} ya {label} ya {weds.upper()}.",
            f"Umelipa: TSh {paid:,}" if paid > 0 else "Umelipa: TSh 0",
            f"BADO: TSh {max(pledge - paid, 0):,}",
        ]

        if info:
            lines.append("")  # Blank line before Malipo
            lines.append(f"Malipo:\n{info}")

        lines.append("")
        lines.append("Tafadhali toa taarifa baada ya kulipa. Asante & Mungu akubariki.")

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

# Sections of the unified "Event Website" admin form.
# field = (key, label, kind, store)   kind: text|area|dt|date|check   store: "col" (Event column) | "json" (site_extra)
FIELD_SECTIONS = [
    ("Website", [("site_enabled", "Show the website (turn ON to publish)", "check", "col")]),
    ("Headline", [
        ("type_label", "Event type shown under the name (e.g. Wedding Celebration, Dinner Party, Send-Off). Leave empty to use the event type.", "text", "json"),
        ("tagline", "Short tagline under the type (optional)", "text", "json"),
        ("city", "City / location (optional)", "text", "json"),
        ("hashtag", "Hashtag (optional)", "text", "json"),
        ("wedding_dt", "Event date & time (East Africa Time) — enables the countdown and Save the Date", "dt", "json"),
        ("initials", "Monogram initials (optional; made from the event name if empty)", "text", "json")]),
    ("Story / welcome", [
        ("story_title", "Heading (optional)", "text", "json"),
        ("site_story", "Welcome message (separate paragraphs with a blank line)", "area", "col"),
        ("portrait", "Portrait photo URL (optional; defaults to the first gallery photo)", "text", "json")]),
    ("Programme & venues", [
        ("cer_title", "First section heading (e.g. The Ceremony / The Dinner)", "text", "json"),
        ("cer_name", "Venue name", "text", "json"), ("cer_addr", "Full address", "text", "json"),
        ("cer_dt", "Date & time (leave empty to use the event date)", "dt", "json"),
        ("cer_maps", "Google Maps link (optional; built from the address if empty)", "text", "json"),
        ("rec_title", "Second section heading (e.g. The Reception) — leave all second-section fields empty to hide it", "text", "json"),
        ("rec_name", "Venue name", "text", "json"), ("rec_addr", "Full address", "text", "json"),
        ("rec_dt", "Date & time", "dt", "json"), ("rec_maps", "Google Maps link (optional)", "text", "json")]),
    ("Dress code & colours", [
        ("site_dress_code", "Dress code (e.g. Elegant & celebratory)", "text", "col"),
        ("dress_note", "Attire note (optional)", "area", "json"),
        ("palette", "Colours, one per line:  Name #HEX   (e.g. Dusty Rose #b77e78). Weddings get a default palette if empty.", "area", "json")]),
    ("Order of the day", [("schedule", "One per line:  9:30 AM | Guest arrival | Short description (optional)", "area", "json")]),
    ("RSVP & contact", [
        ("rsvp_deadline", "RSVP deadline (e.g. 1 June 2027)", "date", "json"),
        ("wa", "WhatsApp number for RSVP — international digits only, e.g. 255712345678", "text", "json"),
        ("email", "Contact email (optional)", "text", "json"),
        ("site_contact", "Contact details shown on the page (names and phone numbers)", "area", "col"),
        ("instagram", "Instagram URL (optional)", "text", "json")]),
    ("Contributions (michango)", [("contribution_info", "How to contribute: payment numbers and names (also used in contribution reminders)", "area", "col")]),
    ("FAQ & maps", [
        ("faq", "FAQ, one per line:  Question | Answer", "area", "json"),
        ("embeds", "Embedded map URLs (optional; Google Maps 'Embed a map' src links, one per line)", "area", "json")]),
]

_TYPE_LABEL = {"Wedding": "Wedding Celebration", "Send-Off": "Send-Off Celebration", "Birthday": "Birthday Celebration",
               "Conference": "Conference", "Confirmation": "Confirmation Celebration", "Corporate": "Corporate Event", "Other": "Special Celebration"}
_COPY_WEDDING = dict(
    tagline="A celebration of love, laughter & forever", story_title="Our story is bringing us to you.", story_kicker="Two hearts, one beautiful story",
    story="Some love stories feel like home from the very beginning. Ours has been filled with little moments, big dreams, laughter, friendship, and a love that keeps growing.\n\nNow, we are so excited to begin our next chapter as one, and even more excited to celebrate this special day with the people who have made our journey so meaningful.\n\nHaving you with us would make our day unforgettable.",
    sign="With love", details_h="The wedding details", countdown_h="Until we say \u201cI do\u201d", palette_h="Our wedding palette",
    palette_p="A soft, romantic blend inspired by gardens, candlelight and timeless elegance.",
    palette_note="Feel free to use these colours as inspiration for your outfit. Most importantly, come as you are and celebrate with us.",
    b1="The Ceremony", b2="The Reception", title_tag="A Celebration of Love")
_COPY_EVENT = dict(
    tagline="", story_title="You're warmly invited", story_kicker="About this occasion",
    story="We are delighted to invite you to celebrate with us. Your presence would make this occasion truly special, and we can't wait to welcome you.",
    sign="Warm regards", details_h="The event details", countdown_h="Until the big day", palette_h="Our colours",
    palette_p="The colours we'll be celebrating in.", palette_note="Feel free to wear these colours if you like. Most importantly, come and celebrate with us.",
    b1="The Event", b2="Afterwards", title_tag="You're Invited")


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


def build_wedding(ev, guest_name="", dev=False, rsvp=None):
    """Public-only view model. Never includes guests, phones (other than the configured RSVP
    number) or admin data."""
    x = _wextra(ev)
    g = lambda k: (x.get(k) or "").strip()
    bride = groom = ""
    etype = (getattr(ev, "event_type", None) or "Wedding").strip()
    wedding = etype in ("Wedding", "Send-Off")
    C = _COPY_WEDDING if wedding else _COPY_EVENT
    base = (getattr(ev, "weds_names", None) or ev.name or "").strip()
    names = f"{bride} & {groom}" if bride and groom else (base or "Our Event")
    initials = _re.sub(r"[^A-Za-z0-9&]", "", g("initials") or "".join(p[0] for p in names.replace("&", " ").split()[:2]).upper())[:3] or "W"
    gallery = _site_gallery(ev)
    hero = getattr(ev, "site_hero_url", None) or ""
    wd = _wdt(g("wedding_dt")) or _wdt(g("cer_dt"))
    legacy = ", ".join(p for p in [getattr(ev, "event_day", None), getattr(ev, "event_date", None)] if p)
    date_str = _wfmt(wd)[0] or legacy or "Date to be confirmed"

    def block(p, fallback_name=""):
        d = _wdt(g(p + "_dt")) or (wd if p == "cer" else None)
        ds, ts, _sh = _wfmt(d)
        b = dict(title=g(p + "_title") or (C["b1"] if p == "cer" else C["b2"]), name=g(p + "_name") or fallback_name, addr=g(p + "_addr"), date=ds, time=ts,
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
    msg = (f"Hello! My name is {guest_name or '[your name]'}. I would love to confirm my attendance at "
           f"{names}. Number of guests: [number]. Thank you!")
    parts = [bride, groom] if bride and groom else [p.strip() for p in _re.split(r"\s*(?:&|\band\b|\bna\b)\s*", base) if p.strip()]
    if len(parts) != 2:
        parts = [names]
    ph = getattr(ev, "site_contact", None) or ""
    return dict(
        slug=ev.slug, names=names, initials=initials, tagline=g("tagline") or C["tagline"],
        place=g("city") or (getattr(ev, "event_venue", None) or ""), hashtag=g("hashtag"),
        date_str=date_str, iso=wd.isoformat() if wd else "", has_date=bool(wd),
        hero=hero, portrait=_safe_url(g("portrait")) or (gallery[0] if gallery else hero),
        story=getattr(ev, "site_story", None) or C["story"], gallery=gallery, cer=cer, rec=rec,
        dress=getattr(ev, "site_dress_code", None) or "", dress_note=g("dress_note"),
        deadline=g("rsvp_deadline"), palette=palette or (_DEFAULT_PALETTE if wedding else []),
        faq=faq, embeds=[u for u in g("embeds").splitlines() if u.strip().startswith("https://www.google.com/maps/embed")],
        wa=wa, wa_href=(f"https://wa.me/{wa}?text={_q(msg)}" if wa else ""),
        email=g("email") if "@" in g("email") else "", instagram=_safe_url(g("instagram")),
        contact_text=ph, c=C, type_label=g("type_label") or _TYPE_LABEL.get(etype, "Special Celebration"), contribute=getattr(ev, "contribution_info", None) or "", guest=guest_name, dev=dev, sample=not x, rsvp=rsvp,
        parts=parts, date_short=_wfmt(wd)[2] or (legacy.upper() if legacy else "DATE TO BE CONFIRMED"), date_long=date_str,
        hero_note=g("quote_hero") or "Love Brings Us Here", cap=g("polaroid_cap") or "Better Together",
        story_title=g("story_title") or C["story_title"],
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


# ---------------------------------------------------------------------------
# Report numbers & billing (family card = 2 people per double-card equivalent)
# ---------------------------------------------------------------------------
import math as _math
from xml.sax.saxutils import escape as _xesc


def _hm(dt):
    if not dt:
        return ""
    if getattr(dt, "tzinfo", None) is not None:
        dt = dt.astimezone(_EAT)
    return dt.strftime("%H:%M")


def _units(n):
    """People -> double-card equivalents: 20 people = 10, 5 people = 3 (rounded up)."""
    return max(1, _math.ceil(max(1, int(n or 1)) / 2))


def build_report(guests, basis="issued", rate_single=0, rate_double=0):
    """Single source of truth for the report page, the PDF and the bill.
    basis 'issued'   = bill every card that was issued.
    basis 'attended' = bill only cards that were scanned at least once (family units from people who entered)."""
    ctype = lambda g: (g.card_type or "single").lower() if (g.card_type or "single").lower() in ("single", "double", "family") else "single"
    size = lambda g: max(1, int(g.group_size or 1))
    used = lambda g: int(g.checked_in_count or 0)
    guests = list(guests)
    singles = [g for g in guests if ctype(g) == "single"]
    doubles = [g for g in guests if ctype(g) == "double"]
    fams = [g for g in guests if ctype(g) == "family"]
    fam_rows = [dict(vid=str(g.visual_id or 0).zfill(4), name=g.name or "", people=size(g), units=_units(size(g)),
                     entered=used(g), units_in=(_units(used(g)) if used(g) >= 1 else 0)) for g in fams]
    issued = dict(single=len(singles), double=len(doubles), family=len(fams), family_people=sum(size(g) for g in fams),
                  family_units=sum(r["units"] for r in fam_rows))
    attended = dict(single=sum(1 for g in singles if used(g) >= 1), double=sum(1 for g in doubles if used(g) >= 1),
                    family=sum(1 for g in fams if used(g) >= 1), family_people=sum(used(g) for g in fams),
                    family_units=sum(r["units_in"] for r in fam_rows))
    base = attended if basis == "attended" else issued
    rs, rd = int(rate_single or 0), int(rate_double or 0)
    lines = [dict(label="Single cards", qty=base["single"], rate=rs, amount=base["single"] * rs),
             dict(label="Double cards", qty=base["double"], rate=rd, amount=base["double"] * rd)]
    if issued["family"]:
        lines.append(dict(label="Family / group cards (2 people = 1 double card)", qty=base["family_units"], rate=rd,
                          amount=base["family_units"] * rd))
    entered = [g for g in guests if used(g) >= 1]
    return dict(
        basis=basis, issued=issued, attended=attended, lines=lines, rates=dict(single=rs, double=rd),
        total_qty=sum(l["qty"] for l in lines), total=sum(l["amount"] for l in lines),
        cards=len(guests), people_allowed=sum(size(g) for g in guests), people_entered=sum(used(g) for g in guests),
        cards_attended=len(entered), no_shows=len(guests) - len(entered),
        rsvp_yes=sum(1 for g in guests if g.rsvp_status == "attending"), rsvp_no=sum(1 for g in guests if g.rsvp_status == "not_attending"),
        pledged=sum(int(getattr(g, "pledge_amount", 0) or 0) for g in guests), paid=sum(int(getattr(g, "paid_amount", 0) or 0) for g in guests),
        family=fam_rows,
        entered_list=[dict(vid=str(g.visual_id or 0).zfill(4), name=g.name or "", type=ctype(g), size=size(g), used=used(g),
                           time=_hm(g.entry_time)) for g in entered],
        not_entered_list=[dict(vid=str(g.visual_id or 0).zfill(4), name=g.name or "", type=ctype(g), size=size(g),
                               rsvp=g.rsvp_status or "") for g in guests if used(g) == 0])


def _tsh(n):
    return f"{int(n or 0):,}"


def build_client_pdf(ev, rep, generated):
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.lib import colors
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable, KeepTogether
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.enums import TA_CENTER, TA_RIGHT
    GREEN, GOLD, GREY, INK, LIGHT, BORDER = (colors.HexColor(x) for x in ("#185a3f", "#c9a84c", "#6b7280", "#1f2937", "#f0f7f4", "#d1d5db"))
    W = A4[0] - 36 * mm
    ps = lambda n, **k: ParagraphStyle(n, fontName=k.pop("fontName", "Helvetica"), **k)
    S_T, S_M = ps("t", fontName="Helvetica-Bold", fontSize=21, leading=26, textColor=GREEN, alignment=TA_CENTER, spaceAfter=4), ps("m", fontSize=9, leading=12, textColor=GREY, alignment=TA_CENTER, spaceAfter=10)
    S_H = ps("h", fontName="Helvetica-Bold", fontSize=12.5, textColor=GREEN, spaceBefore=14, spaceAfter=5)
    S_C, S_CB = ps("c", fontSize=8.6, textColor=INK, leading=11.5), ps("cb", fontName="Helvetica-Bold", fontSize=8.6, textColor=INK, leading=11.5)
    S_HD, S_N = ps("hd", fontName="Helvetica-Bold", fontSize=8, textColor=colors.white), ps("n", fontSize=8, textColor=GREY, leading=11)
    S_R = ps("r", fontSize=8.6, textColor=INK, alignment=TA_RIGHT)
    S_RB = ps("rb", fontName="Helvetica-Bold", fontSize=10, textColor=INK, alignment=TA_RIGHT)
    P = lambda t, s=S_C: Paragraph(_xesc(str(t)), s)
    base_style = [("BACKGROUND", (0, 0), (-1, 0), GREEN), ("GRID", (0, 0), (-1, -1), .3, BORDER), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                  ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4), ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#fafafa")])]
    def table(rows, widths, extra=()):
        t = Table(rows, colWidths=widths, repeatRows=1)
        t.setStyle(TableStyle(base_style + list(extra)))
        return t

    name = (getattr(ev, "weds_names", None) or getattr(ev, "name", "") or "Event") if ev else "Event"
    meta = " · ".join(x for x in [getattr(ev, "event_day", None), getattr(ev, "event_date", None), getattr(ev, "event_venue", None)] if x) if ev else ""
    basis_txt = "cards that attended (scanned at least once)" if rep["basis"] == "attended" else "all cards issued"
    story = [Spacer(1, 4 * mm), Paragraph("Event Report &amp; Invoice Summary", S_T), Paragraph(_xesc(name), ps("nm", fontName="Helvetica-Bold", fontSize=12.5, leading=16, textColor=INK, alignment=TA_CENTER, spaceAfter=2)),
             Paragraph(_xesc(meta + ("  ·  " if meta else "") + f"Prepared {generated}"), S_M), HRFlowable(width="100%", thickness=1.5, color=GOLD, spaceAfter=6)]

    # 1. Billing
    story.append(Paragraph("1. Billing summary", S_H))
    rows = [[Paragraph(h, S_HD) for h in ("Item", "Quantity", "Rate (TSh)", "Amount (TSh)")]]
    for l in rep["lines"]:
        rows.append([P(l["label"]), P(l["qty"], S_R), P(_tsh(l["rate"]) if l["rate"] else "—", S_R), P(_tsh(l["amount"]) if l["rate"] else "—", S_R)])
    rows.append([P("TOTAL", S_CB), P(rep["total_qty"], S_RB), P("", S_R), P(("TSh " + _tsh(rep["total"])) if (rep["rates"]["single"] or rep["rates"]["double"]) else "Rates not set", S_RB)])
    story.append(table(rows, [W * .50, W * .14, W * .16, W * .20], [("BACKGROUND", (0, -1), (-1, -1), LIGHT), ("LINEABOVE", (0, -1), (-1, -1), 1, GREEN)]))
    story.append(Spacer(1, 3))
    story.append(Paragraph(f"Billed on: <b>{basis_txt}</b>. A family/group card is counted as double cards: every 2 people allowed (or entered, when billing on attendance) = 1 double card, rounded up.", S_N))
    if rep["family"]:
        story.append(Paragraph("Family / group card conversion", S_H))
        rows = [[Paragraph(h, S_HD) for h in ("#", "Guest", "People allowed", "= Double cards", "People entered", "= Double cards")]]
        for r in rep["family"]:
            rows.append([P(r["vid"]), P(r["name"]), P(r["people"], S_R), P(r["units"], S_R), P(r["entered"], S_R), P(r["units_in"], S_R)])
        i = rep["issued"]; a = rep["attended"]
        rows.append([P("", S_CB), P("Total", S_CB), P(i["family_people"], S_RB), P(i["family_units"], S_RB), P(a["family_people"], S_RB), P(a["family_units"], S_RB)])
        story.append(table(rows, [W * .09, W * .31, W * .15, W * .15, W * .15, W * .15], [("BACKGROUND", (0, -1), (-1, -1), LIGHT)]))

    # 2. Attendance
    story.append(Paragraph("2. Cards and attendance", S_H))
    i, a = rep["issued"], rep["attended"]
    rows = [[Paragraph(h, S_HD) for h in ("Card type", "Cards issued", "Cards used (scanned)", "People allowed", "People entered")]]
    sp = lambda n: n
    rows.append([P("Single"), P(i["single"], S_R), P(a["single"], S_R), P(i["single"], S_R), P(a["single"], S_R)])
    rows.append([P("Double"), P(i["double"], S_R), P(a["double"], S_R), P(i["double"] * 2, S_R), P("—", S_R)])
    rows.append([P("Family / group"), P(i["family"], S_R), P(a["family"], S_R), P(i["family_people"], S_R), P(a["family_people"], S_R)])
    rows.append([P("TOTAL", S_CB), P(rep["cards"], S_RB), P(rep["cards_attended"], S_RB), P(rep["people_allowed"], S_RB), P(rep["people_entered"], S_RB)])
    story.append(table(rows, [W * .24, W * .17, W * .21, W * .19, W * .19], [("BACKGROUND", (0, -1), (-1, -1), LIGHT)]))
    story.append(Spacer(1, 3))
    story.append(Paragraph(f"Total people who entered (all scans): <b>{rep['people_entered']}</b> of {rep['people_allowed']} allowed · cards not used: <b>{rep['no_shows']}</b> · RSVP confirmed: {rep['rsvp_yes']} · declined: {rep['rsvp_no']}.", S_N))
    if rep["pledged"]:
        story.append(Paragraph(f"Contributions: pledged TSh {_tsh(rep['pledged'])} · received TSh {_tsh(rep['paid'])} · outstanding TSh {_tsh(max(rep['pledged'] - rep['paid'], 0))}.", S_N))

    # 3/4 lists
    story.append(Paragraph(f"3. Guests checked in — {len(rep['entered_list'])} card(s), {rep['people_entered']} people", S_H))
    rows = [[Paragraph(h, S_HD) for h in ("#", "Guest", "Card", "Entered / allowed", "Time")]]
    rows += [[P(g["vid"], S_CB), P(g["name"]), P(g["type"].title()), P(f'{g["used"]} / {g["size"]}', S_R), P(g["time"])] for g in rep["entered_list"]] or [[P("No guests have checked in yet."), "", "", "", ""]]
    story.append(table(rows, [W * .1, W * .42, W * .14, W * .18, W * .16], [("SPAN", (0, 1), (-1, 1))] if not rep["entered_list"] else []))
    story.append(Paragraph(f"4. Not yet checked in — {len(rep['not_entered_list'])} card(s)", S_H))
    rows = [[Paragraph(h, S_HD) for h in ("#", "Guest", "Card", "Allowed", "RSVP")]]
    lab = {"attending": "Confirmed", "not_attending": "Declined"}
    rows += [[P(g["vid"], S_CB), P(g["name"]), P(g["type"].title()), P(g["size"], S_R), P(lab.get(g["rsvp"], "—"))] for g in rep["not_entered_list"]] or [[P("All guests have checked in."), "", "", "", ""]]
    story.append(table(rows, [W * .1, W * .42, W * .14, W * .16, W * .18], [("SPAN", (0, 1), (-1, 1))] if not rep["not_entered_list"] else []))
    story += [Spacer(1, 8), HRFlowable(width="100%", thickness=.4, color=BORDER), Paragraph("Generated automatically by SwiftInvite. Guest data is confidential.", ps("ft", fontSize=7.5, textColor=GREY, alignment=TA_CENTER, spaceBefore=4))]

    def deco(c, d):
        c.saveState(); w, h = A4
        c.setFillColor(GREEN); c.rect(0, h - 10 * mm, w, 10 * mm, fill=1, stroke=0)
        c.setFillColor(colors.white); c.setFont("Helvetica-Bold", 8.5); c.drawCentredString(w / 2, h - 6.8 * mm, "EVENT REPORT — CONFIDENTIAL")
        c.setFillColor(GREY); c.setFont("Helvetica", 7); c.drawString(18 * mm, 6 * mm, f"SwiftInvite · {generated}"); c.drawRightString(w - 18 * mm, 6 * mm, f"Page {d.page}")
        c.restoreState()
    buf = BytesIO()
    SimpleDocTemplate(buf, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=18 * mm, bottomMargin=14 * mm, title="Event Report", author="SwiftInvite").build(story, onFirstPage=deco, onLaterPages=deco)
    return buf.getvalue()


def register_extras(app, *, get_active_event, now_eat, to_whatsapp_number,
                    upload_to_supabase, delete_from_supabase,
                    login_required, admin_required,
                    at_send_sms, at_configured):

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
                extra = {}
                for _title, fields in FIELD_SECTIONS:
                    for key, _label, kind, store in fields:
                        if kind == "check":
                            setattr(ev, key, key in request.form)
                            continue
                        val = (request.form.get(key) or "").strip()[:4000]
                        if store == "col":
                            setattr(ev, key, val or None)
                        else:
                            extra[key] = val
                ev.site_extra = json.dumps(extra)
                try:
                    hero = request.files.get("hero")
                    if hero and hero.filename:
                        ev.site_hero_url = _upload_site_image(ev, hero, "hero", upload_to_supabase, 1800)
                    gallery = _site_gallery(ev)
                    for i, f in enumerate(request.files.getlist("images")):
                        if not f or not f.filename:
                            continue
                        if len(gallery) >= SITE_MAX_GALLERY:
                            flash(f"Gallery limit is {SITE_MAX_GALLERY} photos; extra files ignored.", "warning")
                            break
                        gallery.append(_upload_site_image(ev, f, f"g{i}", upload_to_supabase))
                    ev.site_gallery = json.dumps(gallery)
                except Exception as e:
                    msg = str(e)
                    if "Bucket not found" in msg or "404" in msg:
                        flash(f'Storage bucket "{SITE_BUCKET}" not found. Create it in Supabase (Storage -> New bucket, Public).', "danger")
                    else:
                        flash(f"Image upload failed: {msg}", "danger")
                    logging.exception("site image upload failed")
                db.commit()
                flash("Event website saved.", "success")
                return redirect(url_for("event_site_edit", event_id=event_id))
            x = _wextra(ev)
            vals = {}
            for _title, fields in FIELD_SECTIONS:
                for key, _label, kind, store in fields:
                    vals[key] = bool(getattr(ev, key, False)) if kind == "check" else ((getattr(ev, key, None) or "") if store == "col" else x.get(key, ""))
            data = dict(id=ev.id, name=ev.name, slug=ev.slug, title=ev.weds_names or ev.name, etype=ev.event_type or "Wedding",
                        day=ev.event_day or "", date=ev.event_date or "", venue=ev.event_venue or "",
                        hero=getattr(ev, "site_hero_url", None) or "", gallery=_site_gallery(ev))
            public_url = f"{PUBLIC_BASE_URL or request.host_url.rstrip('/')}/e/{ev.slug}"
        return render_template("event_site_form.html", event=data, sections=FIELD_SECTIONS, vals=vals, public_url=public_url)

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
                gate=bool(getattr(ev, "contrib_gate", False)),
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
            g = db.get(Guest, int(d["guest_id"])) if d.get("guest_id") else None
            if d.get("guest_id") and not g:
                return jsonify(success=False, error="Guest not found"), 404
            if g and kind == "personal" and (g.pledge_amount or 0) <= (g.paid_amount or 0):
                return jsonify(success=False, error=f"{g.name} has no outstanding balance.")
            text = build_reminder_sms(g or _Sample(), ev, kind, d.get("when_text", ""))
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
                if getattr(ev, "contrib_gate", False):
                    _cleared = card_status(g, ev)[0]
                    if (kind == "event" and not _cleared) or (kind == "michango" and _cleared):
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
                         ctype=(g.card_type or "single").lower(), pledge=g.pledge_amount or 0, paid=g.paid_amount or 0,
                         override=bool(getattr(g, "card_override", False)), sent=bool(g.whatsapp_sent or g.at_sms_sent)) for g in gs]
            cfg = dict(gate=bool(getattr(ev, "contrib_gate", False)), single=getattr(ev, "min_single", 0) or 0,
                       double=getattr(ev, "min_double", 0) or 0, group=getattr(ev, "min_group", 0) or 0,
                       gate_scan=bool(getattr(ev, "gate_scan", False)))
            name = ev.name if ev else "No Event"
        return render_template("pledges.html", rows=rows, cfg=cfg, event_name=name)

    @app.route("/pledges/settings", methods=["POST"])
    @admin_required
    def pledges_settings():
        with get_db_session() as db:
            ev = get_active_event(db)
            if ev:
                ev.min_single = _num(request.form.get("min_single"))
                ev.min_double = _num(request.form.get("min_double"))
                ev.min_group = _num(request.form.get("min_group"))
                ev.contrib_gate = "contrib_gate" in request.form
                ev.gate_scan = "gate_scan" in request.form
                db.commit()
                flash("Contribution rules saved.", "success")
        return redirect(url_for("pledges_page"))

    @app.route("/pledges/override/<int:gid>", methods=["POST"])
    @admin_required
    def pledge_override(gid):
        d = request.get_json() or {}
        with get_db_session() as db:
            g = db.get(Guest, gid)
            if not g:
                return jsonify(success=False, error="Guest not found"), 404
            g.card_override = bool(d.get("on"))
            db.commit()
            return jsonify(success=True, override=g.card_override)

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
        gname, rsvp = "", None
        _g, _s = request.args.get("g", ""), request.args.get("s", "")
        if _sig_ok(_g, _s):
            with get_db_session() as db2:
                _guest = db2.query(Guest).filter_by(qr_code_id=_g, event_id=ev.id).first()
                gname = (_guest.name or "").strip() if _guest else ""
                if _guest:
                    rsvp = {"g": _g, "s": _s, "status": _guest.rsvp_status or ""}
        dev = bool(app.debug or os.getenv("FLASK_ENV") == "development")
        w = build_wedding(ev, gname, dev, rsvp)
        base = PUBLIC_BASE_URL or request.host_url.rstrip("/")
        w["url"] = f"{base}/w/{ev.slug}"
        resp = app.make_response(render_template("wedding_bwg.html", w=w))
        resp.headers["Cache-Control"] = "private, no-store" if gname else "public, max-age=60"
        return resp

    @app.route("/e/<slug>")
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
            body = build_ics(w['names'], start, loc, (PUBLIC_BASE_URL or request.host_url.rstrip("/")) + f"/w/{slug}")
        return app.response_class(body, mimetype="text/calendar",
                                  headers={"Content-Disposition": f"attachment; filename=wedding-{slug}.ics"})

    @app.route("/events/<int:event_id>/site/wedding")
    @admin_required
    def event_wedding_edit(event_id):
        return redirect(url_for("event_site_edit", event_id=event_id))

    # ── Website RSVP (same fields as the WhatsApp webhook) ───────────────
    @app.route("/w/<slug>/rsvp", methods=["POST"])
    def public_wedding_rsvp(slug):
        d = request.get_json(silent=True) or {}
        g, sig, status = str(d.get("g", "")), str(d.get("s", "")), d.get("status")
        if status not in ("attending", "not_attending") or not _sig_ok(g, sig):
            return jsonify(success=False, error="invalid"), 400
        with get_db_session() as db:
            ev = db.query(Event).filter_by(slug=slug).first()
            guest = db.query(Guest).filter_by(qr_code_id=g, event_id=ev.id).first() if ev else None
            if not ev or not getattr(ev, "site_enabled", False) or not guest:
                return jsonify(success=False, error="not_found"), 404
            guest.rsvp_status = status
            guest.rsvp_at = now_eat()
            db.commit()
            return jsonify(success=True, status=status)

    # ── Personal website links (admin) — SMS no longer carries a link ─────
    @app.route("/guest_links")
    @admin_required
    def guest_links_page():
        with get_db_session() as db:
            ev = get_active_event(db)
            gs = db.query(Guest).filter_by(event_id=ev.id if ev else None).order_by(Guest.visual_id).all()
            base = PUBLIC_BASE_URL or request.host_url.rstrip("/")
            rows = [dict(vid=str(g.visual_id or 0).zfill(4), name=g.name or "", phone=g.phone or "", rsvp=g.rsvp_status or "",
                         url=f"{base}/e/{ev.slug}?g={g.qr_code_id}&s={_guest_sig(g.qr_code_id)}") for g in gs if ev]
            name = ev.name if ev else "No Event"
            on = bool(ev and getattr(ev, "site_enabled", False))
        return render_template("guest_links.html", rows=rows, event_name=name, site_on=on)

    # ── Template helper: card release status for the active event ────────
    @app.context_processor
    def inject_contrib():
        if not session.get("logged_in"):
            return {}
        try:
            with get_db_session() as db:
                ev = get_active_event(db)
                cfg = types.SimpleNamespace(contrib_gate=bool(getattr(ev, "contrib_gate", False)),
                                            min_single=getattr(ev, "min_single", 0) or 0,
                                            min_double=getattr(ev, "min_double", 0) or 0,
                                            min_group=getattr(ev, "min_group", 0) or 0) if ev else None
        except Exception:
            return {}
        return {"contrib_gate": bool(cfg and cfg.contrib_gate), "card_state": (lambda g: card_status(g, cfg))}

    # ── Report, billing and client PDF ──────────────────────────────────
    def _report_inputs():
        basis = "attended" if request.args.get("basis") == "attended" else "issued"
        with get_db_session() as db:
            ev = get_active_event(db)
            gs = db.query(Guest).filter_by(event_id=ev.id if ev else None).order_by(Guest.visual_id).all()
            rs, rd = int(getattr(ev, "price_single", 0) or 0), int(getattr(ev, "price_double", 0) or 0)
            if "single" in request.args: rs = _num(request.args["single"])
            if "double" in request.args: rd = _num(request.args["double"])
            rep = build_report(gs, basis, rs, rd)
            info = dict(name=(ev.weds_names or ev.name) if ev else "", day=getattr(ev, "event_day", "") or "",
                        date=getattr(ev, "event_date", "") or "", venue=getattr(ev, "event_venue", "") or "",
                        slug=getattr(ev, "slug", "event"))
            ev_snapshot = types.SimpleNamespace(weds_names=info["name"], name=info["name"], event_day=info["day"], event_date=info["date"], event_venue=info["venue"])
        return rep, info, ev_snapshot

    @app.route("/report_summary")
    @login_required
    def report_summary():
        rep, info, _ev = _report_inputs()
        rep["event"] = info
        return jsonify(rep)

    @app.route("/report/rates", methods=["POST"])
    @admin_required
    def report_rates():
        d = request.get_json() or {}
        with get_db_session() as db:
            ev = get_active_event(db)
            if not ev:
                return jsonify(success=False, error="No event"), 400
            ev.price_single, ev.price_double = _num(d.get("single")), _num(d.get("double"))
            db.commit()
            return jsonify(success=True, single=ev.price_single, double=ev.price_double)

    @app.route("/client_report")
    @login_required
    def client_report_pdf():
        rep, info, ev = _report_inputs()
        stamp = now_eat().strftime("%d %B %Y, %H:%M")
        pdf = build_client_pdf(ev, rep, stamp)
        return app.response_class(pdf, mimetype="application/pdf", headers={
            "Content-Disposition": f"attachment; filename=Event_Report_{info['slug']}_{now_eat().strftime('%Y%m%d_%H%M')}.pdf"})

    # ── Live numbers for the scan screen ────────────────────────────────
    @app.route("/scan_stats")
    @login_required
    def scan_stats():
        with get_db_session() as db:
            ev = get_active_event(db)
            gs = db.query(Guest).filter_by(event_id=ev.id if ev else None).all()
            return jsonify(cards=len(gs), cards_in=sum(1 for g in gs if (g.checked_in_count or 0) >= 1),
                           people_in=sum(int(g.checked_in_count or 0) for g in gs),
                           people_allowed=sum(max(1, int(g.group_size or 1)) for g in gs))

    # ── WhatsApp health check (admin) ───────────────────────────────────
    @app.route("/whatsapp_status")
    @admin_required
    def whatsapp_status():
        import requests
        with get_db_session() as db:
            ev = get_active_event(db)
            ev_pid, ev_tok = getattr(ev, "wa_phone_number_id", None), getattr(ev, "wa_access_token", None)
            pid, tok = ev_pid or os.getenv("WHATSAPP_PHONE_NUMBER_ID") or "", ev_tok or os.getenv("WHATSAPP_ACCESS_TOKEN") or ""
            ev_name = ev.name if ev else ""
        info, tier, error, code = {}, "", "", None
        if not pid or not tok:
            error = "The Phone Number ID or the access token is not set."
        else:
            hdr = {"Authorization": f"Bearer {tok}"}
            try:
                r = requests.get(f"https://graph.facebook.com/v21.0/{pid}", headers=hdr, timeout=15, params={
                    "fields": "display_phone_number,verified_name,name_status,quality_rating,platform_type,account_mode,code_verification_status,status"})
                d = r.json()
                if r.ok:
                    info = d
                    try:  # separate call: not every token/app may read this field
                        t = requests.get(f"https://graph.facebook.com/v21.0/{pid}", headers=hdr, timeout=15,
                                         params={"fields": "whatsapp_business_manager_messaging_limit"}).json()
                        tier = t.get("whatsapp_business_manager_messaging_limit", "")
                    except Exception:
                        pass
                else:
                    e = d.get("error", {})
                    error, code = e.get("message", "Meta rejected the request."), e.get("code")
            except Exception as exc:
                error = f"Could not reach Meta: {exc}"
        ns = (info.get("name_status") or "").upper()
        advice = {
            "APPROVED": "Meta approved the display name. If chats still show only the number, follow the checklist below (re-register after a name change, business verification, test on a phone that never chatted with you).",
            "PENDING_REVIEW": "The display name is still being reviewed by Meta. It cannot show until it is approved.",
            "AVAILABLE_WITHOUT_REVIEW": "Meta has NOT put this name through display-name review, so it is not approved and will not show in chats. Submit it for review with the button below.",
            "DECLINED": "Meta declined the display name. Edit it so it matches your business name and website exactly, then submit again.",
            "EXPIRED": "The name approval expired. Submit the display name again in WhatsApp Manager.",
            "NONE": "No display name is registered on this number.",
        }.get(ns, "")
        return render_template("whatsapp_status.html", info=info, tier=tier, error=error, code=code, advice=advice, event_name=ev_name,
                               pid=pid, event_token=bool(ev_tok), event_pid=bool(ev_pid))

    # ── WhatsApp fixes: submit the display name / re-register the number ──
    def _wa_call(path, payload, as_json=False):
        import requests
        with get_db_session() as db:
            ev = get_active_event(db)
            pid = getattr(ev, "wa_phone_number_id", None) or os.getenv("WHATSAPP_PHONE_NUMBER_ID") or ""
            tok = getattr(ev, "wa_access_token", None) or os.getenv("WHATSAPP_ACCESS_TOKEN") or ""
        if not pid or not tok:
            return False, "Phone Number ID or access token is not set."
        kw = {"json": payload} if as_json else {"data": payload}
        try:
            r = requests.post(f"https://graph.facebook.com/v21.0/{pid}{path}", headers={"Authorization": f"Bearer {tok}"}, timeout=25, **kw)
            d = r.json()
        except Exception as exc:
            return False, f"Could not reach Meta: {exc}"
        if r.ok and d.get("success", True):
            return True, "Meta accepted the request."
        e = d.get("error", {})
        return False, f"{e.get('message', 'Meta rejected the request.')} (code {e.get('code')})"

    @app.route("/whatsapp_status/name", methods=["POST"])
    @admin_required
    def whatsapp_submit_name():
        name = (request.form.get("name") or "").strip()[:75]
        if not name:
            flash("Type the display name first.", "danger")
        else:
            ok, msg = _wa_call("", {"new_display_name": name})
            flash(f"Display name '{name}' submitted for review. {msg} Check back in 1-2 days." if ok else f"Not submitted: {msg}", "success" if ok else "danger")
        return redirect(url_for("whatsapp_status"))

    @app.route("/whatsapp_status/register", methods=["POST"])
    @admin_required
    def whatsapp_register():
        pin = _re.sub(r"\D", "", request.form.get("pin") or "")
        if len(pin) != 6:
            flash("The PIN must be exactly 6 digits (the two-step verification PIN).", "danger")
        else:
            ok, msg = _wa_call("/register", {"messaging_product": "whatsapp", "pin": pin}, as_json=True)
            flash("Number registered. Refresh this page to see the new status." if ok else f"Registration failed: {msg}", "success" if ok else "danger")
        return redirect(url_for("whatsapp_status"))

    @app.route("/reminder_guests")
    @admin_required
    def reminder_guests():
        q = (request.args.get("q") or "").strip().lower()
        with get_db_session() as db:
            ev = get_active_event(db)
            gs = db.query(Guest).filter_by(event_id=ev.id if ev else None).order_by(Guest.visual_id).all()
            out = []
            for g in gs:
                hay = f"{g.name or ''} {g.phone or ''} {g.visual_id or ''}".lower()
                if q and q not in hay:
                    continue
                pl, pd = int(g.pledge_amount or 0), int(g.paid_amount or 0)
                out.append(dict(id=g.id, vid=str(g.visual_id or 0).zfill(4), name=g.name or "", phone=g.phone or "",
                                ctype=(g.card_type or "single").title(), pledge=pl, paid=pd, balance=max(pl - pd, 0),
                                last=_hm(g.reminder_sms_sent_at) if getattr(g, "reminder_sms_sent_at", None) else ""))
                if len(out) >= 15:
                    break
        return jsonify(guests=out)