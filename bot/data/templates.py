"""Refactored from bot.utils.storage: Message template storage and formatting."""
from bot.data.state import Any, Dict, _mark_settings_key_dirty, _storage, save_storage, thread_safe
from bot.data.languages import DEFAULT_LANGUAGE

def _custom_entry(template_name: str) -> dict:
    """Return the per-language custom template map for a template (or ``{}``).

    Backwards compatible: a legacy flat string entry is treated as the
    English-only custom template.
    """
    custom = _storage.get("message_templates", {})
    entry = custom.get(template_name)
    if isinstance(entry, dict):
        return entry
    if isinstance(entry, str):
        return {DEFAULT_LANGUAGE: entry}
    return {}

def _template_source_lang(template_name: str) -> str:
    """Return the language the admin last uploaded a template in.

    Recorded by the auto-translation service when a template is saved through
    the admin panel. Falls back to English when unknown.
    """
    try:
        meta = _storage.get("message_templates_meta") or {}
        entry = meta.get(template_name)
        if isinstance(entry, dict):
            source = entry.get("source")
            if source in _storage.get("message_templates", {}).get(template_name, {}):
                return source
    except Exception:
        pass
    return DEFAULT_LANGUAGE

def get_message_template(template_name: str, lang: str = DEFAULT_LANGUAGE) -> str:
    """Get a message template for a language.

    Once the admin customizes a template in ANY language, that custom version
    is the source of truth for EVERY language. Per-language overrides are
    honored; languages that have not been translated yet fall back to the
    source language the admin wrote the template in, then English.

    Resolution order for a customized template:
      1. admin-custom template for ``lang`` (per-language override)
      2. admin-custom template in the source language
      3. admin-custom English template
      4. any other admin-custom language

    Resolution order for a stock (never customized) template:
      1. built-in translation dictionary for ``lang``
      2. built-in translation dictionary English entry
      3. built-in default English text
    """
    entry = _custom_entry(template_name)
    if lang in entry:
        return entry[lang]
    if entry:
        source = _template_source_lang(template_name)
        if source in entry:
            return entry[source]
        if DEFAULT_LANGUAGE in entry:
            return entry[DEFAULT_LANGUAGE]
        return next(iter(entry.values()))
    try:
        from bot.data.translations import TRANSLATIONS
        by_lang = TRANSLATIONS.get(template_name)
        if by_lang:
            text = by_lang.get(lang)
            if text is not None:
                return text
            text = by_lang.get(DEFAULT_LANGUAGE)
            if text is not None:
                return text
    except (ImportError, KeyError):
        pass
    return DEFAULT_MESSAGE_TEMPLATES.get(template_name, f"[{template_name}]")

def set_message_template(template_name: str, template_text: str, lang: str = DEFAULT_LANGUAGE) -> None:
    """Set a custom message template for a language (admin override).

    The template becomes admin-customized: ``get_message_template`` will show
    this version to users of every language (falling back to the source
    language until the auto-translation fills the other languages).
    """
    if "message_templates" not in _storage:
        _storage["message_templates"] = {}
    entry = _custom_entry(template_name)
    entry[lang] = template_text
    _storage["message_templates"][template_name] = entry
    _mark_settings_key_dirty("message_templates")
    save_storage()

    try:
        from bot.utils.fast_cache import preload_templates as _refresh
        _refresh()
    except (ImportError, Exception):
        pass

def _drop_template_meta(template_name: str) -> None:
    """Remove the auto-translation metadata for a template, if any."""
    meta = _storage.get("message_templates_meta")
    if meta and isinstance(meta, dict):
        meta.pop(template_name, None)
        _storage["message_templates_meta"] = meta
        _mark_settings_key_dirty("message_templates_meta")

def reset_message_template(template_name: str, lang: str = None) -> None:
    """Reset a message template (one language, or all) to default.

    ``lang=None`` removes the whole custom template. Passing a language code
    removes only that language's override, keeping other languages' custom text.

    Resetting the source language (the one the template was last written in)
    also drops the stale auto-translations that were derived from it, keeping
    only languages the admin edited by hand - otherwise those languages would
    keep showing text translated from the old source.
    """
    entry = _custom_entry(template_name)
    if not entry:
        return
    if lang is None:
        if "message_templates" in _storage:
            _storage["message_templates"].pop(template_name, None)
        _drop_template_meta(template_name)
    else:
        source = _template_source_lang(template_name)
        entry.pop(lang, None)
        if lang == source:
            try:
                meta = _storage.get("message_templates_meta") or {}
                meta_entry = meta.get(template_name) or {}
                manual = set(meta_entry.get("manual") or [])
            except Exception:
                manual = set()
            kept = {k: v for k, v in entry.items() if k in manual}
            if kept:
                _storage["message_templates"][template_name] = kept
            else:
                _storage["message_templates"].pop(template_name, None)
            _drop_template_meta(template_name)
        elif entry:
            _storage["message_templates"][template_name] = entry
        else:
            _storage["message_templates"].pop(template_name, None)
            _drop_template_meta(template_name)
    _mark_settings_key_dirty("message_templates")
    save_storage()

    try:
        from bot.utils.fast_cache import preload_templates as _refresh
        _refresh()
    except (ImportError, Exception):
        pass

def get_all_message_templates() -> Dict[str, Dict[str, Any]]:
    """Get all message templates with their status."""
    result = {}

    for name, default_text in DEFAULT_MESSAGE_TEMPLATES.items():
        entry = _custom_entry(name)
        is_custom = bool(entry)
        current_text = get_message_template(name) if is_custom else default_text
        result[name] = {
            "current": current_text,
            "default": default_text,
            "is_custom": is_custom,
            "custom": entry,
        }

    return result

def format_message(template_name: str, lang: str = DEFAULT_LANGUAGE, **kwargs) -> str:
    """Format a message template with variables (safe - no overlapping key corruption)."""
    import re
    template = get_message_template(template_name, lang)
    try:
        for key, value in kwargs.items():
            template = re.sub(r'\{' + re.escape(key) + r'\}', lambda m: str(value), template)
        return template
    except Exception:
        return template

DEFAULT_MESSAGE_TEMPLATES = {
    "account_received_message": "{flag} The number {phone} has been successfully registered and is now waiting for confirmation.\n\n\u23f3 Confirmation Time: {timeout} seconds\n\n\u26a0\ufe0f Important: Please log out of your account on all other devices to ensure a smooth confirmation process.",
    "success_message": "\U0001f389 We have successfully processed your account\n\nNumber: {phone}\nPrice: {price}\nStatus: \u2714 {status}\nCongratulations, has been added to your balance.",
    "multi_device_message": "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n\u00a0 \u26a0\ufe0f\u00a0 Multiple Devices Detected\n\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\nNumber : {phone}\nYour account is logged in from more\nthan one device.\n\nVerification is paused for 24 hours.\n\nPlease log out of all other devices\nto continue.\n\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501",
    "support_message": "\U0001f4ccTo join the all Uorope series Bots notification channel, please request access using the link below \u2b07\ufe0f\n\n\U0001f4e3\U0001f4ce https://t.me/virtualnumbernews1\n\u00a0\u00a0\u00a0\u00a0\u00a0\u00a0 https://t.me/virtualnumbernews1\n\n\U0001f553If your joining request is not approved within an hour and you are unable to join or use the bot, please contact our support team\u2714\n\n\U0001f468\u200d\U0001f4bb Support Team:\u00a0 @Xrd_Didox \U0001f3ea\n\n\n\n\U0001f449 https://t.me/virtualnumbernews1\n\u00a0\u00a0\u00a0\u00a0\u00a0\u00a0 https://t.me/virtualnumbernews1\n\n\u274cPlease note that in order to use the bot, you must be a member of the notification private channel.If you are not a member of the private channel,you cannot work.So please be a member of the private channel first then start working.\U0001f4e2\n\n\n\U0001f4c8\U0001f4c8\U0001f4c8\U0001f4c8\U0001f4c8\U0001f4c8\U0001f4c8\U0001f4c8\U0001f4c8\U0001f4c8",
    "account_stats_message": "\U0001f194 ID : {user_id}\n\n\U0001f4f1 Number of verified accounts : {verified}\n\U0001f4f1 Number of unverified accounts : {unverified}\n\U0001f4b8 Available Balance IN USDT : {available_balance}\n\U0001f9ca Number of frozen account : {frozen}\n\u2744\ufe0f Total Frozen Balance : {frozen_balance}\n\n\U0001f4c5 Date : {date}\n\u23f0 Time : {time}\n\n\U0001f4dd Note : Work, be honest Upgrade your work and earn money and enjoy your life\n     /start",
    "verification_failed_message": "VERIFICATION STATUS\n\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n\u26a0\ufe0f Verification could not be completed\n\U0001f4f1 Number: {phone}\n\U0001f310 Region: {country_code} {flag}\n\nWe couldn\u2019t detect an active login for this number.\nIf you intended to continue, open Telegram, send /start to this bot, and try again.",
    "welcome_message": "\U0001f389 \U0001d686\U0001d68e\U0001d695\U0001d68c\U0001d698\U0001d696\U0001d68e \U0001d69d\U0001d698 \U0001d681\U0001d698\U0001d68b\U0001d698\U0001d69d!\n\n\U0001d674\U0001d697\U0001d69d\U0001d68e\U0001d69b \U0001d6a2\U0001d698\U0001d69e\U0001d69b \U0001d699\U0001d691\U0001d698\U0001d697\U0001d68e \U0001d697\U0001d69e\U0001d696\U0001d68b\U0001d68e\U0001d69b \U0001d6a0\U0001d692\U0001d69d\U0001d691 \U0001d69d\U0001d691\U0001d68e \U0001d68c\U0001d698\U0001d69e\U0001d697\U0001d69d\U0001d69b\U0001d6a2 \U0001d68c\U0001d698\U0001d68d\U0001d68e.\n\U0001d674\U0001d6a1\U0001d68a\U0001d696\U0001d699\U0001d695\U0001d68e: +\U0001d7fc\U0001d7f8\U0001d6a1\U0001d6a1\U0001d6a1\U0001d6a1\U0001d6a1\U0001d6a1\U0001d6a1\n\n\U0001d683\U0001d6a2\U0001d699\U0001d68e /cap \U0001d69d\U0001d698 \U0001d69c\U0001d68e\U0001d68e \U0001d68a\U0001d69f\U0001d68a\U0001d692\U0001d695\U0001d68a\U0001d68b\U0001d695\U0001d68e \U0001d68c\U0001d698\U0001d69e\U0001d697\U0001d69d\U0001d69b\U0001d692\U0001d68e\U0001d69c.",
    "invalid_otp_message": "\u2718 *Invalid OTP Code*\n\nThe code you entered is incorrect.\nPlease enter the OTP sent to your Telegram app.",
    "processing_message": "\u23f3 *Processing...*\n\nPlease wait while we process your request.\nThis may take a few moments.",
    "banned_user_message": "\U0001f6ab *Access Denied*\n\nYou have been banned from using this bot.\nContact support if you believe this is a mistake.",
    "withdraw_success_message": "\u2714 *Withdraw Request Submitted*\n\n\U0001f516 Transaction ID: `{transaction_id}`\n\n\U0001f4b0 Amount: *${amount}*\n\U0001f4b3 Method: *{method}*\n\U0001faaa {wallet_label}: `{wallet_address}`\n\n\U0001f4c5 Date: {date}\n\U0001f552 Time: {time}\n\n\U0001f4ca *Balance Breakdown:*\n   \u2022 Total Numbers: {total_numbers}\n   \u2022 Countries: {countries}\n   \u2022 Total Balance: ${total_balance}\n\n\nYour request will be processed shortly. Check /account for updated balance.",
    "no_balance_message": "\u2718 *Insufficient Balance*\n\nYou do not have any balance to withdraw.\n\nSubmit numbers via /start to earn balance.",
    "twofa_already_active_message": "\u26a0\ufe0f *2FA Already Enabled*\n\nThis account already has Two-Factor Authentication.\n\nUse /start to try a different number.",
    "enter_otp_message": "\U0001f522 *Enter OTP Code*\n\nEnter the code sent to your Telegram app.\n\n\u23f0 Timeout: *{timeout} seconds*\n\nType /cancel to stop.",
    "withdraw_disabled_message": "\u2718 *Withdrawals Unavailable*\n\nWithdrawals are temporarily disabled.\nPlease check back later.",
    "withdraw_method_message": "\U0001f4b3 *Select Withdrawal Method*\n\n\U0001f4b0 Your Balance: *${balance}*\n\nPlease choose your preferred method:",
    "enter_wallet_message": "\U0001f4ec *Enter {method} Wallet Address*\n\nSend your {method} wallet address below.\n\n\u26a0\ufe0f *Important:*\n\u2022 Double-check the address before sending\n\u2022 Incorrect addresses cannot be refunded\n\u2022 Type /cancel to abort",
    "invalid_wallet_message": "\u2718 *Invalid Wallet Address*\n\nError: `{error}`\n\n\U0001f4ec Please send a valid *{method}* address.\n\nType /cancel to abort.",
    "card_unavailable_message": "This card is not available.\nPlease contact your admin.\nOtherwise, contact this bot support.",
    "join_channel_message": "\U0001f4e2 *Channel Required*\n\n\U0001f517 Channel: {channel}\n\nYou must join this channel to use the bot.\n\n\u2714 After joining, click the button below\nor type /start to begin.",
    "proxy_not_working_contact_support": "\u26a0\ufe0f *Proxy problem for this country.*\n\nThe proxy for {country_code} is not working right now, so the code could not be sent.\n\nPlease wait a few minutes and try again, or contact support/admin.",
}
