# whatsapp.py — Meta Cloud API helper using Message Templates

import os
import requests
import logging

WHATSAPP_ACCESS_TOKEN = os.getenv("WHATSAPP_ACCESS_TOKEN")
WHATSAPP_PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID")
WHATSAPP_API_VERSION = "v19.0"
WHATSAPP_API_BASE = f"https://graph.facebook.com/{WHATSAPP_API_VERSION}"

# ── Defaults (used when event has no override) ───────────────────────────────
DEFAULT_TEMPLATE_NAME = "event_invitation"
DEFAULT_TEMPLATE_LANGUAGE = "sw"


def _headers(access_token: str = None):
    token = access_token or WHATSAPP_ACCESS_TOKEN
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def upload_media(image_bytes: bytes, filename: str,
                 mime_type: str = "image/png",
                 phone_number_id: str = None,
                 access_token: str = None) -> str:
    """Upload image to Meta, return media_id."""
    pid = phone_number_id or WHATSAPP_PHONE_NUMBER_ID
    url = f"{WHATSAPP_API_BASE}/{pid}/media"
    headers = {"Authorization": f"Bearer {access_token or WHATSAPP_ACCESS_TOKEN}"}
    files = {"file": (filename, image_bytes, mime_type)}
    data = {"messaging_product": "whatsapp", "type": mime_type}
    response = requests.post(url, headers=headers, files=files, data=data)
    if not response.ok:
        logging.error(f"Media upload failed: {response.status_code} {response.text}")
        response.raise_for_status()
    result = response.json()
    media_id = result.get("id")
    if not media_id:
        raise ValueError(f"No media_id in response: {result}")
    logging.info(f"Uploaded media: {media_id}")
    return media_id


def send_template_message(to: str, guest_name: str, card_number: str,
                          media_id: str,
                          template_name: str = None,
                          template_language: str = None,
                          phone_number_id: str = None,
                          access_token: str = None) -> dict:
    """
    Send a WhatsApp template message.
    Falls back to DEFAULT_TEMPLATE_NAME / DEFAULT_TEMPLATE_LANGUAGE / env vars
    if no per-event overrides are supplied.
    """
    pid = phone_number_id or WHATSAPP_PHONE_NUMBER_ID
    t_name = template_name or DEFAULT_TEMPLATE_NAME
    t_lang = template_language or DEFAULT_TEMPLATE_LANGUAGE

    url = f"{WHATSAPP_API_BASE}/{pid}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "template",
        "template": {
            "name": t_name,
            "language": {"code": t_lang},
            "components": [
                {
                    "type": "header",
                    "parameters": [
                        {"type": "image", "image": {"id": media_id}}
                    ],
                },
                {
                    "type": "body",
                    "parameters": [
                        {"type": "text", "text": guest_name},
                        {"type": "text", "text": card_number},
                    ],
                },
            ],
        },
    }

    response = requests.post(url, headers=_headers(access_token), json=payload)
    if not response.ok:
        error_data = response.json() if response.content else {}
        error_code = (
            error_data.get("error", {}).get("code") or
            error_data.get("error", {}).get("error_data", {}).get("details")
        )
        if response.status_code == 400 and error_code == 131026:
            logging.warning(f"Invalid WhatsApp number: {to}")
            return {"status": "invalid_number", "to": to}
        logging.error(f"Template send failed: {response.status_code} {response.text}")
        response.raise_for_status()

    result = response.json()
    result["status"] = "sent"
    return result


def send_guest_card(to: str, guest_name: str, visual_id: int,
                    card_type: str, image_bytes: bytes, filename: str,
                    template_name: str = None,
                    template_language: str = None,
                    phone_number_id: str = None,
                    access_token: str = None) -> dict:
    """Upload card image then send template. Returns API response."""
    media_id = upload_media(
        image_bytes, filename,
        phone_number_id=phone_number_id,
        access_token=access_token,
    )
    return send_template_message(
        to=to,
        guest_name=guest_name,
        card_number=f"{visual_id:04d}",
        media_id=media_id,
        template_name=template_name,
        template_language=template_language,
        phone_number_id=phone_number_id,
        access_token=access_token,
    )