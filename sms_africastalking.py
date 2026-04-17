"""
sms_africastalking.py — Africa's Talking SMS provider

Required environment variables:
    AT_API_KEY      — your Africa's Talking API key
    AT_USERNAME     — your Africa's Talking username (use 'sandbox' for testing)
    AT_SENDER_ID    — your registered shortcode / sender name (optional but recommended)
"""

import os
import logging
import africastalking

_initialized = False


def _init():
    global _initialized
    if _initialized:
        return
    username = os.getenv("AT_USERNAME")
    api_key  = os.getenv("AT_API_KEY")
    if username and api_key:
        africastalking.initialize(username, api_key)
        _initialized = True


def is_configured() -> bool:
    return bool(os.getenv("AT_API_KEY") and os.getenv("AT_USERNAME"))


def send_sms(phone: str, message: str) -> dict:
    """
    Send a single SMS via Africa's Talking.

    Returns:
        {"success": True}  on success
        {"success": False, "error": "<reason>"}  on failure
    """
    if not is_configured():
        return {"success": False, "error": "Africa's Talking not configured."}

    try:
        _init()
        sms        = africastalking.SMS
        sender_id  = os.getenv("AT_SENDER_ID") or None
        recipients = [f"+{phone}" if not phone.startswith("+") else phone]

        response = sms.send(message, recipients, sender_id=sender_id)
        logging.info(f"AT SMS response: {response}")

        # Africa's Talking wraps results in SMSMessageData → Recipients list
        recipients_data = (
            response.get("SMSMessageData", {}).get("Recipients", [])
        )

        if recipients_data:
            status = recipients_data[0].get("status", "")
            if status == "Success":
                return {"success": True}
            else:
                return {"success": False, "error": status}

        # Fallback: if the top-level message says "Sent"
        top_msg = response.get("SMSMessageData", {}).get("Message", "")
        if "Sent" in top_msg:
            return {"success": True}

        return {"success": False, "error": f"Unexpected response: {response}"}

    except Exception as e:
        logging.error(f"Africa's Talking SMS error: {e}", exc_info=True)
        return {"success": False, "error": str(e)}