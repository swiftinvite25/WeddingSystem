# whatsapp.py — Meta Cloud API helper using Message Templates
import os
import requests
import logging

WHATSAPP_ACCESS_TOKEN = os.getenv("WHATSAPP_ACCESS_TOKEN")
WHATSAPP_PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID")
WHATSAPP_API_VERSION = "v19.0"
WHATSAPP_API_BASE = f"https://graph.facebook.com/{WHATSAPP_API_VERSION}"

TEMPLATE_NAME = "event_invitation"
TEMPLATE_LANGUAGE = "sw"  # Swahili


def _headers():
    return {
        "Authorization": f"Bearer {WHATSAPP_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }


def upload_media(image_bytes: bytes, filename: str, mime_type: str = "image/png") -> str:
    """Upload image to Meta, return media_id."""
    url = f"{WHATSAPP_API_BASE}/{WHATSAPP_PHONE_NUMBER_ID}/media"
    headers = {"Authorization": f"Bearer {WHATSAPP_ACCESS_TOKEN}"}
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


def send_template_message(to: str, guest_name: str, card_number: str, media_id: str) -> dict:
    """
    Send event_invitation template.
    Header  : image
    Body    : {{1}} = guest_name, {{2}} = card_number
    Buttons : Nitakuwepo/I'll Be There | Sitokuwepo/Can't Make It
    """
    url = f"{WHATSAPP_API_BASE}/{WHATSAPP_PHONE_NUMBER_ID}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "template",
        "template": {
            "name": TEMPLATE_NAME,
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
    response = requests.post(url, headers=_headers(), json=payload)
    if not response.ok:
        logging.error(f"Template send failed: {response.status_code} {response.text}")
    response.raise_for_status()
    return response.json()


def send_guest_card(to: str, guest_name: str, visual_id: int,
                    card_type: str, image_bytes: bytes, filename: str) -> dict:
    """Upload card image then send template. Returns API response."""
    media_id = upload_media(image_bytes, filename)
    return send_template_message(
        to=to,
        guest_name=guest_name,
        card_number=f"{visual_id:04d}",
        media_id=media_id,
    )


def check_whatsapp_numbers(phone_numbers: list[str]) -> dict[str, bool]:
    """
    Check which numbers have WhatsApp accounts using the Meta contacts endpoint.
    Accepts a list of numbers in E.164 format (e.g. "255674114407").
    Returns dict: { "255674114407": True, "255700000000": False }

    Note: This endpoint may not be available on all WhatsApp Business API tiers.
    If you receive a 403 or "unsupported" error, use the send-and-detect approach
    instead (check for error code 131026 in send failures).
    """
    url = f"{WHATSAPP_API_BASE}/{WHATSAPP_PHONE_NUMBER_ID}/contacts"
    payload = {
        "messaging_product": "whatsapp",
        "contacts": phone_numbers,
    }
    response = requests.post(url, headers=_headers(), json=payload)
    if not response.ok:
        logging.error(f"Contacts check failed: {response.status_code} {response.text}")
        response.raise_for_status()

    results = response.json().get("contacts", [])
    status_map = {}
    for contact in results:
        number = contact.get("input")
        wa_id = contact.get("wa_id")        # present only if valid WhatsApp number
        status = contact.get("status")      # "valid" or "invalid"
        status_map[number] = (status == "valid" and wa_id is not None)

    # Mark any numbers that weren't returned at all as False
    for number in phone_numbers:
        if number not in status_map:
            status_map[number] = False

    return status_map