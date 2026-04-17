"""
sms_africastalking.py — Africa's Talking SMS provider

Required environment variables:
    AT_API_KEY      — Africa's Talking API key (LIVE or SANDBOX)
    AT_USERNAME     — 'sandbox' or live username
    AT_SENDER_ID    — registered sender ID (ONLY works in LIVE)
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
    api_key = os.getenv("AT_API_KEY")

    if username and api_key:
        africastalking.initialize(username, api_key)
        _initialized = True
    else:
        logging.error("Africa's Talking credentials missing")


def is_configured() -> bool:
    return bool(os.getenv("AT_API_KEY") and os.getenv("AT_USERNAME"))


def send_sms(phone: str, message: str) -> dict:
    """
    Send SMS via Africa's Talking (works for sandbox + live).

    Sandbox:
        - NO sender ID
    Live:
        - sender ID OPTIONAL (must be approved if used)
    """

    if not is_configured():
        return {"success": False, "error": "Africa's Talking not configured."}

    try:
        _init()
    
        logging.info(f"AT mode — username: {os.getenv('AT_USERNAME')}, live: {is_live}, sender_id: {sender_id}")
        logging.info(f"Sending to: {recipients}")
        sms = africastalking.SMS

        # Ensure correct phone format
        recipients = [f"+{phone}" if not phone.startswith("+") else phone]

        # ----------------------------------------------------
        # 🔥 SMART SENDER LOGIC (FIXES YOUR PREVIOUS ERROR)
        # ----------------------------------------------------
        sender_id = os.getenv("AT_SENDER_ID")

        # ONLY use sender ID if:
        # - we are NOT in sandbox
        # - AND sender ID exists
        is_live = os.getenv("AT_USERNAME") != "sandbox"

        kwargs = {}
        if is_live and sender_id:
            kwargs["sender_id"] = sender_id

        response = sms.send(message, recipients, **kwargs)

        logging.info(f"AT SMS response: {response}")

        data = response.get("SMSMessageData", {})
        recipients_data = data.get("Recipients", [])

        # -----------------------------
        # HANDLE RESPONSE SAFELY
        # -----------------------------
        if recipients_data:
            status = recipients_data[0].get("status", "")

            if status.lower() == "success":
                return {
                    "success": True,
                    "message_id": recipients_data[0].get("messageId"),
                    "status": status
                }

            return {
                "success": False,
                "error": status
            }

        # fallback check
        if "Sent" in data.get("Message", ""):
            return {"success": True}

        return {
            "success": False,
            "error": f"Unexpected response: {response}"
        }

    except Exception as e:
        logging.error(f"Africa's Talking SMS error: {e}", exc_info=True)
        return {"success": False, "error": str(e)}