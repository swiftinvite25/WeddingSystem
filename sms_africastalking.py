"""
sms_africastalking.py — Africa's Talking SMS provider

Required environment variables:
    AT_API_KEY      — Africa's Talking API key (LIVE or SANDBOX)
    AT_USERNAME     — 'sandbox' or live username
    AT_SENDER_ID    — registered & approved sender ID (optional, LIVE only)
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

# Known AT error messages returned in the Message field
_AT_KNOWN_ERRORS = {
    "InvalidSenderId",
    "InvalidPhoneNumber",
    "InsufficientBalance",
    "UserInBlacklist",
    "AbsentSubscriber",
    "DeliveryFailure",
}


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
    Live     — sender ID optional (must be approved by AT if used)

    If AT_SENDER_ID is not set or not yet approved, leave it unset in
    your environment and messages will go out via AT's shared shortcode.
    """
    if not is_configured():
        logger.error("Africa's Talking not configured — missing env vars.")
        return {"success": False, "error": "Africa's Talking not configured."}

    try:
        _init()

        sender_id = os.getenv("AT_SENDER_ID", "").strip() or None
        is_live   = os.getenv("AT_USERNAME") != "sandbox"

        logger.info(
            f"AT mode — username: {os.getenv('AT_USERNAME')} | "
            f"live: {is_live} | sender_id: {sender_id or 'none (shared shortcode)'}"
        )

        # Ensure E.164 format
        recipients = [f"+{phone}" if not phone.startswith("+") else phone]
        logger.info(f"Sending SMS to: {recipients}")
        logger.debug(f"Message text: {message}")

        sms = africastalking.SMS

        # Only attach sender_id if live AND it is set
        kwargs = {}
        if is_live and sender_id:
            kwargs["sender_id"] = sender_id
            logger.info(f"Using sender ID: {sender_id}")
        else:
            logger.info(
                "No sender ID attached — "
                + ("sandbox mode." if not is_live else "AT_SENDER_ID not set, using shared shortcode.")
            )

        response = sms.send(message, recipients, **kwargs)
        logger.info(f"AT raw response: {response}")

        data            = response.get("SMSMessageData", {})
        recipients_data = data.get("Recipients", [])
        message_summary = data.get("Message", "")

        logger.info(f"AT message summary: {message_summary}")

        # ── Recipients list returned (normal path) ────────────────────────
        if recipients_data:
            recipient = recipients_data[0]
            status    = recipient.get("status", "")
            number    = recipient.get("number", "")
            cost      = recipient.get("cost", "")
            msg_id    = recipient.get("messageId", "")

            logger.info(
                f"AT recipient — number: {number} | status: {status} | "
                f"cost: {cost} | messageId: {msg_id}"
            )

            if status.lower() == "success":
                return {
                    "success":    True,
                    "message_id": msg_id,
                    "status":     status,
                    "cost":       cost,
                }

            logger.warning(f"AT delivery status not success: '{status}' for {number}")
            return {"success": False, "error": status}

        # ── No recipients list — check message summary ────────────────────
        if "Sent" in message_summary:
            logger.info("AT fallback success via message summary.")
            return {"success": True}

        if message_summary in _AT_KNOWN_ERRORS:
            logger.error(f"AT rejected message with known error: {message_summary}")
            return {"success": False, "error": message_summary}

        logger.warning(f"AT unexpected response: {response}")
        return {"success": False, "error": f"Unexpected AT response: {message_summary or response}"}

    except Exception as e:
        logger.error(f"Africa's Talking SMS exception: {e}", exc_info=True)
        return {"success": False, "error": str(e)}