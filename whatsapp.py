# whatsapp.py — Meta Cloud API helper using Message Templates
import os
import requests
import logging

WHATSAPP_API_VERSION = "v21.0"
WHATSAPP_API_BASE    = f"https://graph.facebook.com/{WHATSAPP_API_VERSION}"

TEMPLATE_NAME     = "event_invitation"
TEMPLATE_LANGUAGE = "sw"  # Swahili


def _token():
    """Read token fresh every call so Render env vars are always current."""
    return os.getenv("WHATSAPP_ACCESS_TOKEN")

def _phone_number_id():
    """Read phone number ID fresh every call."""
    return os.getenv("WHATSAPP_PHONE_NUMBER_ID")

def _headers():
    token = _token()
    if not token:
        raise RuntimeError("WHATSAPP_ACCESS_TOKEN is not set in environment variables.")
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def upload_media(image_bytes: bytes, filename: str, mime_type: str = "image/jpeg") -> str:
    """Upload image to Meta, return media_id."""
    phone_id = _phone_number_id()
    token    = _token()
    if not phone_id:
        raise RuntimeError("WHATSAPP_PHONE_NUMBER_ID is not set in environment variables.")
    if not token:
        raise RuntimeError("WHATSAPP_ACCESS_TOKEN is not set in environment variables.")

    url     = f"{WHATSAPP_API_BASE}/{phone_id}/media"
    headers = {"Authorization": f"Bearer {token}"}
    files   = {"file": (filename, image_bytes, mime_type)}
    data    = {"messaging_product": "whatsapp", "type": mime_type}

    logging.info(f"Uploading media to Meta — file: {filename}, size: {len(image_bytes)} bytes")
    response = requests.post(url, headers=headers, files=files, data=data)

    if not response.ok:
        logging.error(f"Media upload failed: {response.status_code} — {response.text}")
        response.raise_for_status()

    result   = response.json()
    media_id = result.get("id")
    if not media_id:
        raise ValueError(f"No media_id in Meta response: {result}")

    logging.info(f"Media uploaded successfully — media_id: {media_id}")
    return media_id


def send_template_message(to: str, guest_name: str, card_number: str, media_id: str) -> dict:
    """
    Send event_invitation template.
    Header  : image
    Body    : {{1}} = guest_name, {{2}} = card_number
    Buttons : Nitakuwepo / Sitokuwepo
    Returns dict with key 'status': 'sent' | 'invalid_number' | 'failed'
    """
    phone_id = _phone_number_id()
    if not phone_id:
        raise RuntimeError("WHATSAPP_PHONE_NUMBER_ID is not set in environment variables.")

    url = f"{WHATSAPP_API_BASE}/{phone_id}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type":    "individual",
        "to":                to,
        "type":              "template",
        "template": {
            "name":     TEMPLATE_NAME,
            "language": {"code": TEMPLATE_LANGUAGE},
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

    logging.info(f"Sending WhatsApp template to {to} — guest: {guest_name}, card: {card_number}")
    response = requests.post(url, headers=_headers(), json=payload)
    logging.info(f"Meta API response: {response.status_code} — {response.text}")

    if not response.ok:
        error_data = response.json() if response.content else {}
        error_code = error_data.get("error", {}).get("code")
        logging.error(f"Template send failed — code: {error_code} — full error: {error_data}")

        # 131026 = recipient not on WhatsApp
        if response.status_code == 400 and error_code == 131026:
            logging.warning(f"Number not on WhatsApp: {to}")
            return {"status": "invalid_number", "to": to}

        response.raise_for_status()

    result           = response.json()
    result["status"] = "sent"
    logging.info(f"WhatsApp message queued successfully for {to} — response: {result}")
    return result


def send_guest_card(to: str, guest_name: str, visual_id: int,
                    card_type: str, image_bytes: bytes, filename: str) -> dict:
    """Upload card image then send template. Returns API response."""
    logging.info(f"send_guest_card called — to: {to}, guest: {guest_name}, id: {visual_id}")
    media_id = upload_media(image_bytes, filename)
    return send_template_message(
        to=to,
        guest_name=guest_name,
        card_number=f"{visual_id:04d}",
        media_id=media_id,
    )