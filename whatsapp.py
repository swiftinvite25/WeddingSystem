# whatsapp.py — Meta Cloud API helper
# Supports per-event template configs stored as JSON in the Event model.
#
# wa_template_config JSON structure (stored per event):
# {
#   "has_image_header": true,        -- whether template header is an image
#   "body_vars": ["guest_name", "card_number"],  -- ordered list of body variables
#                                       supported vars: guest_name, card_number,
#                                       event_name, site_link
#   "has_buttons": true,             -- whether template has quick-reply buttons
#   "url_button_index": 2            -- OPTIONAL: position (0-based) of a dynamic
#                                       URL button whose variable is the event slug,
#                                       e.g. https://yourapp.com/e/{{1}}
# }
#
# Default (backwards compatible with event_invitation template):
# { "has_image_header": true, "body_vars": ["guest_name", "card_number"], "has_buttons": true }
#
# NOTE: "site_link" and "url_button_index" only take effect if you add them to an
# event's config AFTER Meta has approved a template that contains those variables.
# Existing events/templates are unaffected.

import os
import re
import json
import requests
import logging

WHATSAPP_API_VERSION = "v21.0"
WHATSAPP_API_BASE    = f"https://graph.facebook.com/{WHATSAPP_API_VERSION}"

# Default template config — matches the existing event_invitation template
DEFAULT_TEMPLATE_CONFIG = {
    "has_image_header": True,
    "body_vars":        ["guest_name", "card_number"],
    "has_buttons":      True,
}


def _token(event=None):
    """Per-event token, falls back to global env var."""
    if event and getattr(event, 'wa_access_token', None):
        return event.wa_access_token
    return os.getenv("WHATSAPP_ACCESS_TOKEN")


def _phone_number_id(event=None):
    """Per-event phone number ID, falls back to global env var."""
    if event and getattr(event, 'wa_phone_number_id', None):
        return event.wa_phone_number_id
    return os.getenv("WHATSAPP_PHONE_NUMBER_ID")


def _template_name(event=None):
    if event and getattr(event, 'wa_template_name', None):
        return event.wa_template_name
    return os.getenv("WHATSAPP_TEMPLATE_NAME", "event_invitation")


def _template_language(event=None):
    if event and getattr(event, 'wa_template_language', None):
        return event.wa_template_language
    return os.getenv("WHATSAPP_TEMPLATE_LANGUAGE", "sw")


def _template_config(event=None) -> dict:
    """Parse the per-event template config JSON, or return default."""
    if event and getattr(event, 'wa_template_config', None):
        try:
            return json.loads(event.wa_template_config)
        except (json.JSONDecodeError, TypeError):
            pass
    return DEFAULT_TEMPLATE_CONFIG


def _headers(event=None):
    token = _token(event)
    if not token:
        raise RuntimeError("WHATSAPP_ACCESS_TOKEN is not set.")
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type":  "application/json",
    }


def upload_media(image_bytes: bytes, filename: str,
                 mime_type: str = "image/jpeg", event=None) -> str:
    """Upload image to Meta, return media_id."""
    phone_id = _phone_number_id(event)
    token    = _token(event)
    if not phone_id:
        raise RuntimeError("WHATSAPP_PHONE_NUMBER_ID is not set.")
    if not token:
        raise RuntimeError("WHATSAPP_ACCESS_TOKEN is not set.")

    url     = f"{WHATSAPP_API_BASE}/{phone_id}/media"
    headers = {"Authorization": f"Bearer {token}"}
    files   = {"file": (filename, image_bytes, mime_type)}
    data    = {"messaging_product": "whatsapp", "type": mime_type}

    logging.info(f"Uploading media — file: {filename}, size: {len(image_bytes)} bytes")
    response = requests.post(url, headers=headers, files=files, data=data)

    if not response.ok:
        logging.error(f"Media upload failed: {response.status_code} — {response.text}")
        response.raise_for_status()

    result   = response.json()
    media_id = result.get("id")
    if not media_id:
        raise ValueError(f"No media_id in Meta response: {result}")

    logging.info(f"Media uploaded — media_id: {media_id}")
    return media_id


def _build_components(config: dict, media_id: str | None,
                       guest_name: str, card_number: str,
                       event_name: str = "",
                       site_url: str = "", site_slug: str = "") -> list:
    """Build the template components list from config."""
    components = []

    # Header
    if config.get("has_image_header", True) and media_id:
        components.append({
            "type": "header",
            "parameters": [
                {"type": "image", "image": {"id": media_id}}
            ],
        })

    # Body variables
    var_map = {
        "guest_name":   guest_name,
        "card_number":  card_number,
        "event_name":   event_name,
        # Meta rejects empty parameters, so never send ""
        "site_link":    site_url or "-",
    }
    body_vars = config.get("body_vars", ["guest_name", "card_number"])
    if body_vars:
        components.append({
            "type": "body",
            "parameters": [
                {"type": "text", "text": var_map.get(v, "") or "-"}
                for v in body_vars
            ],
        })

    # Optional dynamic URL button (variable = event slug)
    btn_idx = config.get("url_button_index")
    if btn_idx is not None and site_slug:
        components.append({
            "type":     "button",
            "sub_type": "url",
            "index":    str(btn_idx),
            "parameters": [{"type": "text", "text": site_slug}],
        })

    return components


def send_template_message(to: str, guest_name: str, card_number: str,
                           media_id: str | None, event=None,
                           event_name: str = "",
                           site_url: str = "", site_slug: str = "") -> dict:
    """
    Send a WhatsApp template message.
    Uses per-event config to build the correct component structure.
    Returns dict with 'status': 'sent' | 'invalid_number' | 'failed'
    """
    phone_id = _phone_number_id(event)
    if not phone_id:
        raise RuntimeError("WHATSAPP_PHONE_NUMBER_ID is not set.")

    config   = _template_config(event)
    tmpl     = _template_name(event)
    lang     = _template_language(event)
    comps    = _build_components(config, media_id, guest_name, card_number,
                                 event_name, site_url, site_slug)

    url = f"{WHATSAPP_API_BASE}/{phone_id}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type":    "individual",
        "to":                to,
        "type":              "template",
        "template": {
            "name":       tmpl,
            "language":   {"code": lang},
            "components": comps,
        },
    }

    site_slug = site_slug or ((getattr(event, "slug", "") or "") if event else "")
    logging.info(f"Sending WA template '{tmpl}' to {to} — guest: {guest_name}, card: {card_number}")
    logging.info(f"WA components: {json.dumps(comps)}")
    response = requests.post(url, headers=_headers(event), json=payload)
    logging.info(f"Meta response: {response.status_code} — {response.text}")

    # Self-heal: Meta says the template has a dynamic URL button but the event's
    # template config did not include url_button_index -> add it once and retry.
    if not response.ok and site_slug and not any(c.get("type") == "button" for c in comps):
        try:
            err = response.json().get("error", {})
            details = (err.get("error_data") or {}).get("details", "")
            m = re.search(r"index (\d+) of type Url", details, re.I)
            if err.get("code") == 131008 and m:
                comps.append({"type": "button", "sub_type": "url", "index": m.group(1),
                              "parameters": [{"type": "text", "text": site_slug}]})
                logging.warning(f"Auto-adding URL button index {m.group(1)}. Add "
                                f"\"url_button_index\": {m.group(1)} to this event's template config.")
                response = requests.post(url, headers=_headers(event), json=payload)
                logging.info(f"Meta response (retry): {response.status_code} — {response.text}")
        except Exception as e:
            logging.warning(f"URL-button auto-fix failed: {e}")

    if not response.ok:
        error_data = response.json() if response.content else {}
        error_code = error_data.get("error", {}).get("code")
        logging.error(f"Template send failed — code: {error_code} — {error_data}")

        if response.status_code == 400 and error_code == 131026:
            logging.warning(f"Number not on WhatsApp: {to}")
            return {"status": "invalid_number", "to": to}

        response.raise_for_status()

    result           = response.json()
    result["status"] = "sent"
    logging.info(f"WA message queued for {to} — {result}")
    return result


def send_guest_card(to: str, guest_name: str, visual_id: int,
                    card_type: str, image_bytes: bytes,
                    filename: str, event=None,
                    site_url: str = "", site_slug: str = "") -> dict:
    """Upload card image then send template. Returns API response."""
    logging.info(f"send_guest_card — to: {to}, guest: {guest_name}, id: {visual_id}")
    config   = _template_config(event)
    media_id = None

    if config.get("has_image_header", True) and image_bytes:
        media_id = upload_media(image_bytes, filename, event=event)

    ev_name = getattr(event, 'name', '') if event else ''

    return send_template_message(
        to=to,
        guest_name=guest_name,
        card_number=str(visual_id or 0).zfill(4),
        media_id=media_id,
        event=event,
        event_name=ev_name,
        site_url=site_url,
        site_slug=site_slug,
    )