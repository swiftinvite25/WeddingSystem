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

# ---------------------------------------------------------------------------
# Module-level logger — visible in Render / gunicorn
# ---------------------------------------------------------------------------
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setLevel(logging.DEBUG)
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)
    logger.addHandler(handler)

# ---------------------------------------------------------------------------

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
        logger.info(f"Africa's Talking initialized — username: {username}")
    else:
        logger.error("Africa's Talking credentials missing (AT_USERNAME / AT_API_KEY)")


def is_configured() -> bool:
    return bool(os.getenv("AT_API_KEY") and os.getenv("AT_USERNAME"))


def send_sms(phone: str, message: str) -> dict:
    """
    Send SMS via Africa's Talking (sandbox + live).

    Sandbox  — no sender ID
    Live     — sender ID optional (must be approved if used)
    """
    if not is_configured():
        logger.error("Africa's Talking not configured — missing env vars.")
        return {"success": False, "error": "Africa's Talking not configured."}

    try:
        _init()

        sender_id = os.getenv("AT_SENDER_ID")
        is_live   = os.getenv("AT_USERNAME") != "sandbox"

        logger.info(f"AT mode — username: {os.getenv('AT_USERNAME')} | live: {is_live} | sender_id: {sender_id}")

        # Ensure E.164 format
        recipients = [f"+{phone}" if not phone.startswith("+") else phone]
        logger.info(f"Sending SMS to: {recipients}")
        logger.debug(f"Message text: {message}")

        sms = africastalking.SMS

        kwargs = {}
        if is_live and sender_id:
            kwargs["sender_id"] = sender_id
            logger.info(f"Using sender ID: {sender_id}")
        else:
            logger.info("No sender ID used (sandbox or AT_SENDER_ID not set).")

        response = sms.send(message, recipients, **kwargs)
        logger.info(f"AT raw response: {response}")

        data            = response.get("SMSMessageData", {})
        recipients_data = data.get("Recipients", [])
        message_summary = data.get("Message", "")

        logger.info(f"AT message summary: {message_summary}")

        if recipients_data:
            recipient = recipients_data[0]
            status    = recipient.get("status", "")
            number    = recipient.get("number", "")
            cost      = recipient.get("cost", "")
            msg_id    = recipient.get("messageId", "")

            logger.info(f"AT recipient — number: {number} | status: {status} | cost: {cost} | messageId: {msg_id}")

            if status.lower() == "success":
                return {
                    "success":    True,
                    "message_id": msg_id,
                    "status":     status,
                    "cost":       cost,
                }

            logger.warning(f"AT delivery status not success: '{status}' for {number}")
            return {"success": False, "error": status}

        # Fallback: older AT response format
        if "Sent" in message_summary:
            logger.info("AT fallback success via message summary.")
            return {"success": True}

        logger.warning(f"AT unexpected response structure: {response}")
        return {"success": False, "error": f"Unexpected response: {response}"}

    except Exception as e:
        logger.error(f"Africa's Talking SMS exception: {e}", exc_info=True)
        return {"success": False, "error": str(e)}