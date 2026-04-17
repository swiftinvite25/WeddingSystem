"""
WeblineAfrica SMS helper
API docs: https://webline.africa/api-docs
Endpoint: POST https://sms.webline.africa/api/v3/sms/send
Params:   recipient, sender_id, message  (query params)
Auth:     Bearer token in Authorization header
"""

import os
import requests
import logging

logger = logging.getLogger(__name__)

WEBLINE_API_URL = "https://sms.webline.africa/api/v3/sms/send"
WEBLINE_API_TOKEN = os.environ.get("WEBLINE_API_TOKEN", "")
WEBLINE_SENDER_ID = os.environ.get("WEBLINE_SENDER_ID", "Invite")


def is_configured() -> bool:
    """Return True if the WeblineAfrica token is set in the environment."""
    return bool(WEBLINE_API_TOKEN)


def send_sms(recipient: str, message: str) -> dict:
    """
    Send a single SMS via WeblineAfrica.

    Returns:
        {"success": True,  "message_id": "...", "delivery_status": "SENT"}
        {"success": False, "error": "...reason..."}
    """
    if not is_configured():
        return {"success": False, "error": "WEBLINE_API_TOKEN not configured"}

    # Ensure phone number is in international format
    phone = recipient.strip()
    if not phone.startswith("+"):
        phone = "+" + phone

    try:
        response = requests.post(
            WEBLINE_API_URL,
            params={
                "recipient": phone,
                "sender_id": WEBLINE_SENDER_ID,
                "message": message,
            },
            headers={
                "Authorization": f"Bearer {WEBLINE_API_TOKEN}",
            },
            timeout=15,
        )

        data = response.json()

        if response.status_code == 200 and data.get("status") == "success":
            return {
                "success": True,
                "message_id": data.get("message_id", ""),
                "delivery_status": data.get("delivery_status", "SENT"),
            }
        else:
            error_msg = data.get("message", f"HTTP {response.status_code}")
            logger.warning("WeblineAfrica SMS failed for %s: %s", phone, error_msg)
            return {"success": False, "error": error_msg}

    except requests.exceptions.Timeout:
        return {"success": False, "error": "Request timed out"}
    except requests.exceptions.RequestException as exc:
        logger.exception("WeblineAfrica request error for %s", phone)
        return {"success": False, "error": str(exc)}
    except ValueError:
        # JSON decode error
        return {"success": False, "error": "Invalid response from WeblineAfrica API"}