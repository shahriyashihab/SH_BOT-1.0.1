from pyrogram.client import Client
from pyrogram import filters
from pyrogram import StopPropagation
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from pyrogram.errors import MessageNotModified, FloodWait
from bot.utils.storage import get_min_withdraw_for_method, try_deduct_balance_for_withdraw, save_withdraw_state, load_withdraw_state, clear_withdraw_state
from bot.data.cards import CARD_NAME_TRANSPORT_MAX, CARD_WITHDRAW_METHOD, is_card_available, is_valid_card_name
from bot.config import get_channel

CHANNEL_WITHDRAW_REPORT = get_channel("CHANNEL_WITHDRAW_REPORT")
from bot.handlers.admin.router import NON_TEXT_INPUT_COMMANDS
import asyncio
import re
import threading
import logging
from typing import Tuple
from bot.utils.bg_tasks import spawn_background

logger = logging.getLogger(__name__)

withdraw_sessions = {}
# P23: this lock is held across ``await`` points (callback answers / message
# edits) inside async handlers, so it MUST be an asyncio.Lock. A
# threading.RLock held across ``await`` blocks the event loop and can deadlock
# every other task. asyncio.Lock is created lazily-bound to the running loop.
withdraw_lock = asyncio.Lock()


def validate_wallet_address(address: str, method: str) -> Tuple[bool, str]:
    """
    Validate wallet address based on withdrawal method.

    Args:
        address: The wallet address to validate
        method: The withdrawal method (USDT, TRX, BINANCE, CARDNAME)

    Returns:
        Tuple of (is_valid, error_message or validated_address)

    Only SURROUNDING whitespace is trimmed; the value is returned unchanged
    otherwise. Invalid input is rejected with a concise error - never
    silently converted into a different value.
    """
    address = address.strip()

    if not address:
        return False, "Wallet address cannot be empty"

    # P8-FIX: the old generic 5-200 length gate ran BEFORE method-specific
    # validation and silently rejected valid CARDNAME values such as "BT",
    # "AF", "A6" or "7K" (only 2 chars). Every method now owns its exact
    # rule. Only SURROUNDING whitespace is trimmed above - internal
    # characters are never stripped, so invalid input is rejected, not
    # silently repaired. (The exact anchored regexes below also bound the
    # work per message, preserving the P7-69 DoS fix.)
    method_upper = (method or "").upper()

    if method_upper == "USDT":
        if address.startswith("0x") and len(address) == 42:
            if re.match(r'^0x[a-fA-F0-9]{40}$', address):
                return True, address
            return False, "Invalid BEP20 address format. Should be 0x followed by 40 hex characters."

        if address.startswith("T") and len(address) == 34:
            if re.match(r'^T[a-zA-Z0-9]{33}$', address):
                if _tron_address_checksum_valid(address):
                    return True, address
                return False, "Invalid TRC20 address (checksum failed). Double-check the address."
            return False, "Invalid TRC20 address format."

        return False, "Invalid USDT address. Use BEP20 (0x...) or TRC20 (T...) format."

    elif method_upper == "TRX":
        if address.startswith("T") and len(address) == 34:
            if re.match(r'^T[a-zA-Z0-9]{33}$', address):
                if _tron_address_checksum_valid(address):
                    return True, address
                return False, "Invalid TRX address (checksum failed). Double-check the address."
            return False, "Invalid TRX address format."
        return False, "Invalid TRX address. Should start with 'T' and be 34 characters."

    elif method_upper == "BINANCE":
        # P8-FIX: exact rule - 9 or 10 digits, nothing else. The old broad
        # pattern accepted usernames, emails and symbols.
        if re.match(r'^\d{9,10}$', address):
            return True, address
        return False, "Invalid Binance ID. Must be 9 or 10 digits only."

    elif method_upper == CARD_WITHDRAW_METHOD:
        # P8-FIX: exact business rule - minimum 2 characters, uppercase A-Z
        # and digits only, at least one uppercase letter required (digits
        # alone are invalid). No BUSINESS maximum; CARD_NAME_TRANSPORT_MAX
        # is a separate, documented Telegram-transport bound (screens and
        # reports embed the full name). No spaces, dots, hyphens,
        # underscores, lowercase or special characters.
        # Single source of truth: the shared CARD_NAME_PATTERN lives in
        # bot.data.cards so user-side and admin-side validation can never
        # drift apart.
        if len(address) > CARD_NAME_TRANSPORT_MAX:
            return False, "Card name is too long (Telegram display limit). Send a shorter name."
        if is_valid_card_name(address):
            return True, address
        return False, "Invalid card name. Use 2+ characters: uppercase A-Z and digits, with at least one letter."

    # Unknown method: legacy behavior preserved (bounded + sanitized).
    if len(address) > 200:
        return False, "Wallet address is too long (max 200 characters)"

    dangerous_chars = ['<', '>', '"', "'", '\\', '\n', '\r', '\t', ';', '`', '$', '|', '&']
    for char in dangerous_chars:
        address = address.replace(char, '')

    return True, address


def _tron_address_checksum_valid(address: str) -> bool:
    """Verify the Base58Check-style checksum of a TRON (T...) address.

    P7-69: the format regex alone accepted every T<33 chars> string - a single
    typo silently redirected the payout to a dead address. TRON addresses are
    0x41-prefixed EIP-55-style hex encoded in Base58Check; without any external
    dependency we re-derive the hex and verify the last 4 bytes are the
    SHA-256(SHA-256(payload)) checksum. Never raises.
    """
    try:
        import hashlib

        # Base58 decode (bitcoin alphabet, not base32!).
        alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
        num = 0
        for ch in address:
            idx = alphabet.find(ch)
            if idx < 0:
                return False
            num = num * 58 + idx
        raw = num.to_bytes((num.bit_length() + 7) // 8, "big")
        # Leading '1' characters are leading zero bytes.
        pad = len(address) - len(address.lstrip("1"))
        raw = b"\x00" * pad + raw

        if len(raw) != 25 or raw[0] != 0x41:
            return False
        payload, checksum = raw[:21], raw[21:]
        derived = hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]
        return derived == checksum
    except Exception:
        return False


def _withdraw_wallet_label(method: str) -> str:
    """Human label for the destination field, per withdrawal method.

    BINANCE -> "Binance ID"; CARDNAME -> "Card"; USDT/TRX -> "Wallet Address".
    Used by the request-submitted template so the shown label matches how the
    user is actually being paid (and by the admin paying out that method).
    """
    m = (method or "").upper()
    if m == "BINANCE":
        return "Binance ID"
    if m == CARD_WITHDRAW_METHOD:
        return "Card"
    return "Wallet Address"


def _withdraw_datetime_strings():
    """Local (admin-tz) date and time strings for the withdraw message."""
    from datetime import datetime as _dt
    try:
        from bot.config import ADMIN_TZ
        try:
            from zoneinfo import ZoneInfo
        except ImportError:
            from backports.zoneinfo import ZoneInfo
        now = _dt.utcnow().replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo(ADMIN_TZ))
    except Exception:
        now = _dt.utcnow()
    return now.strftime("%d %B %Y"), now.strftime("%H:%M:%S (%Z)")


def _withdraw_countries_string(breakdown) -> str:
    """Render the per-country breakdown for the request message.

    Format: ``Bangladesh (+880) - 6, Bhutan (+975) - 3`` (biggest first).
    """
    if not breakdown or not isinstance(breakdown, dict):
        return ""
    countries = breakdown.get("countries") or {}
    if not countries:
        return ""
    try:
        from bot.utils.country_data import get_country_name
    except Exception:
        get_country_name = lambda cc: cc  # noqa: E731
    parts = []
    for cc, info in sorted(countries.items(), key=lambda kv: -kv[1].get("total", 0)):
        if cc == "other":
            name = "Other/Bonus"
        else:
            try:
                name = get_country_name(cc)
            except Exception:
                name = cc
        parts.append(f"{name} ({cc}) - {info.get('count', 0)}")
    return ", ".join(parts)


async def register_withdraw(client: Client, message: Message):
    """
    Handles /withdraw command - WITH CHANNEL GUARD
    Shows user balance and withdrawal options
    """
    user_id = message.chat.id
    
    from bot.utils.fast_cache import (
        is_user_banned_fast,
        get_cached_template,
        get_user_balance_fast,
        is_withdraw_enabled_fast,
        background_refresh_withdraw_status,
        sync_user_from_storage,
        format_message_fast
    )
    from bot.utils.i18n import get_user_lang, user_t

    sync_user_from_storage(user_id)
    
    if is_user_banned_fast(user_id):
        await message.reply(get_cached_template("banned_user_message", get_user_lang(user_id)))
        return
    
    if not is_withdraw_enabled_fast():
        await message.reply(get_cached_template("withdraw_disabled_message", get_user_lang(user_id)))
        spawn_background(background_refresh_withdraw_status(), "withdraw-status-refresh")
        return
    
    balance = get_user_balance_fast(user_id)
    
    from bot.utils.storage import get_user_withdraw_config
    user_config = get_user_withdraw_config(user_id)
    
    if user_config.get("disabled"):
        await message.reply(user_t("withdraw_disabled_by_admin", user_id))
        return
    
    if balance <= 0:
        await message.reply(get_cached_template("no_balance_message", get_user_lang(user_id)))
        return
    
    text = format_message_fast("withdraw_method_message", get_user_lang(user_id), balance=balance)
    buttons = [
        [InlineKeyboardButton("USDT (BEP20)", callback_data="withdraw_usdt")],
        [InlineKeyboardButton("TRX", callback_data="withdraw_trx")],
        [InlineKeyboardButton("CARD NAME", callback_data="withdraw_cardname")],
        [InlineKeyboardButton("Binance", callback_data="withdraw_binance")]
    ]
    await message.reply(text, reply_markup=InlineKeyboardMarkup(buttons))
    
    async with withdraw_lock:
        withdraw_sessions[user_id] = {"balance": balance, "step": "waiting_method"}
        # Persist so the flow survives a bot restart.
        save_withdraw_state(user_id, withdraw_sessions[user_id])


def register_withdraw_handlers(app: Client):
    """Register all withdraw-related handlers - WITH CHANNEL GUARD"""

    @app.on_callback_query(filters.regex(r"^withdraw_(usdt|trx|cardname|binance)$"))
    async def handle_withdraw_selection(client: Client, query: CallbackQuery):
        """Withdraw selection - channel membership is enforced by the universal guard.

        CRITICAL: this handler must ALWAYS answer the callback query, otherwise the
        button on the user's client keeps spinning and no message ever appears.
        """
        user_id = query.from_user.id
        from bot.utils.i18n import user_t
        try:
            async with withdraw_lock:
                if user_id not in withdraw_sessions:
                    # Rehydrate an in-progress flow persisted before a restart.
                    saved = load_withdraw_state(user_id)
                    if not saved:
                        await query.answer(user_t("withdraw_start_alert", user_id), show_alert=True)
                        return
                    withdraw_sessions[user_id] = saved

                data_str = str(query.data) if query.data else "withdraw_unknown"
                parts = data_str.split("_")
                method = parts[1].upper() if len(parts) > 1 else "UNKNOWN"

                from bot.utils.fast_cache import get_user_balance_fast
                current_balance = get_user_balance_fast(user_id)

                withdraw_sessions[user_id]["balance"] = current_balance

                min_amount = get_min_withdraw_for_method(method.lower())

                from bot.utils.storage import get_user_withdraw_config
                user_config = get_user_withdraw_config(user_id)

                custom_min = user_config.get("min")
                if custom_min is not None:
                    min_amount = custom_min

                custom_max = user_config.get("max")
                if custom_max is not None and current_balance > custom_max:
                    await query.answer(user_t("withdraw_max_alert", user_id, max_amount=custom_max), show_alert=True)
                    return

                if current_balance < min_amount:
                    await query.answer(user_t("withdraw_insufficient_alert", user_id, min_amount=min_amount, current_balance=current_balance), show_alert=True)
                    return

                if current_balance <= 0:
                    await query.answer(user_t("withdraw_no_balance_alert", user_id), show_alert=True)
                    return

                withdraw_sessions[user_id]["method"] = method
                withdraw_sessions[user_id]["step"] = "waiting_wallet"
                save_withdraw_state(user_id, withdraw_sessions[user_id])

            # Answer FIRST so the button loading state always clears, then edit.
            await query.answer()
            from bot.utils.fast_cache import format_message_fast as fmt_msg
            from bot.utils.i18n import get_user_lang
            _lang = get_user_lang(user_id)
            try:
                await query.message.edit_text(fmt_msg("enter_wallet_message", _lang, method=method))
            except MessageNotModified:
                pass
            except FloodWait as fw:
                wait = int(fw.value) if isinstance(fw.value, (int, float)) else 2
                logger.warning(f"Withdraw selection FloodWait: waiting {wait}s before edit")
                await asyncio.sleep(wait)
                try:
                    await query.message.edit_text(fmt_msg("enter_wallet_message", _lang, method=method))
                except Exception as retry_err:
                    logger.warning(f"Withdraw selection edit retry failed: {retry_err}")
            except Exception as edit_err:
                logger.warning(f"Withdraw selection edit failed (query already answered): {edit_err}")
        except Exception as e:
            logger.error(f"ERROR in handle_withdraw_selection: {e}", exc_info=True)
            try:
                await query.answer(user_t("withdraw_error_alert", query.from_user.id), show_alert=True)
            except Exception:
                pass

    @app.on_message(filters.private & filters.text & ~filters.command(NON_TEXT_INPUT_COMMANDS))
    async def handle_wallet_input(client: Client, message: Message):
        """Wallet input - channel membership is enforced by the universal guard."""
        user_id = message.chat.id
        from bot.utils.i18n import user_t, get_user_lang
        try:
            async with withdraw_lock:
                if user_id not in withdraw_sessions or withdraw_sessions[user_id].get("step") != "waiting_wallet":
                    # Rehydrate an in-progress flow persisted before a restart.
                    if user_id not in withdraw_sessions:
                        saved = load_withdraw_state(user_id)
                        if saved and saved.get("step") == "waiting_wallet":
                            withdraw_sessions[user_id] = saved
                        else:
                            return
                    else:
                        return

                wallet_address = message.text.strip()

                if user_id not in withdraw_sessions:
                    return

                data = withdraw_sessions[user_id]
                method = data.get("method", "UNKNOWN")

                is_valid, result = validate_wallet_address(wallet_address, method)

                if not is_valid:
                    from bot.utils.fast_cache import format_message_fast as fmt_msg
                    await message.reply(fmt_msg("invalid_wallet_message", get_user_lang(user_id), error=result, method=method))
                    message.stop_propagation()
                    return

                wallet_address = result

                # P8-FIX: only ACTIVE card names may be withdrawn with. The
                # registry gate runs BEFORE any balance check or deduction -
                # an unknown or blocked card never deducts, never creates a
                # request and never counts toward card statistics. The
                # session stays in the waiting_wallet step so the user can
                # simply send a corrected card name.
                if method.upper() == CARD_WITHDRAW_METHOD:
                    from bot.data.cards import is_card_available
                    if not is_card_available(wallet_address):
                        await message.reply(
                            user_t("card_unavailable_message", user_id)
                        )
                        message.stop_propagation()
                        return

                # Re-validate the CURRENT balance before deducting. The balance may
                # have changed since the method was selected (e.g. a new session was
                # approved), and the withdraw flow deducts the full available amount.
                from bot.utils.fast_cache import get_user_balance_fast
                current_balance = get_user_balance_fast(user_id)

                min_amount = get_min_withdraw_for_method(method.lower())
                from bot.utils.storage import get_user_withdraw_config
                user_config = get_user_withdraw_config(user_id)
                custom_min = user_config.get("min")
                if custom_min is not None:
                    min_amount = custom_min

                # P7-69: recheck custom_max at the WALLET stage too. The old
                # code checked it only at method selection - a balance change
                # (or admin config change) between the two steps let a user
                # withdraw more than the configured per-withdraw cap.
                custom_max = user_config.get("max")
                if custom_max is not None and current_balance > custom_max:
                    await message.reply(
                        user_t("withdraw_max_alert", user_id, max_amount=custom_max)
                    )
                    message.stop_propagation()
                    return

                if current_balance <= 0 or current_balance < min_amount:
                    await message.reply(
                        user_t("withdraw_failed_insufficient", user_id, min_amount=min_amount, current_balance=current_balance)
                    )
                    message.stop_propagation()
                    return

                # F-6 FIX: the balance deduction AND the pending request record
                # now commit together in one atomic data-layer call. The old
                # flow deducted here and recorded inside the spawned background
                # task - a crash between the two steps silently swallowed the
                # user's money (balance gone, no request anywhere).
                from bot.data.withdrawals import (
                    build_country_breakdown,
                    create_withdraw_request_atomic,
                )
                breakdown = build_country_breakdown(user_id, expected_total=current_balance)
                created = create_withdraw_request_atomic(
                    user_id, method, current_balance, wallet_address,
                    country_breakdown=breakdown,
                )
                if not created.get("success"):
                    await message.reply(user_t("withdraw_failed_generic", user_id))
                    message.stop_propagation()
                    return

                balance = current_balance
                data = withdraw_sessions.pop(user_id)
                method = data["method"]
                clear_withdraw_state(user_id)

            from bot.utils.fast_cache import format_message_fast
            _tx_id = created.get("transaction_id", created.get("request_id", ""))
            # Build the method-aware wallet label (Binance -> "Binance ID",
            # Card -> "Card", TRX/USDT -> "Wallet Address", etc.) and the
            # balance-breakdown fields for the request-submitted message.
            _wallet_label = _withdraw_wallet_label(method)
            _date_str, _time_str = _withdraw_datetime_strings()
            _bd = breakdown or {}
            _total_numbers = _bd.get("numbers", 0)
            try:
                _total_balance = float(_bd.get("total", balance) or 0.0)
            except (TypeError, ValueError):
                _total_balance = float(balance or 0.0)
            _countries_str = _withdraw_countries_string(_bd)
            _success_text = format_message_fast(
                "withdraw_success_message", get_user_lang(user_id),
                transaction_id=_tx_id,
                amount=f"{float(balance):.2f}",
                method=method,
                wallet_label=_wallet_label,
                wallet_address=wallet_address,
                date=_date_str,
                time=_time_str,
                total_numbers=_total_numbers,
                countries=_countries_str,
                total_balance=f"{_total_balance:.2f}",
            )
            await message.reply(_success_text)

            spawn_background(
                process_withdraw_background(client, user_id, balance, method, wallet_address, breakdown),
                "withdraw-process",
            )

            message.stop_propagation()
        except StopPropagation:
            raise
        except Exception as e:
            logger.error(f"ERROR in handle_wallet_input: {e}", exc_info=True)
            try:
                await message.reply(user_t("withdraw_wallet_error", message.chat.id))
            except Exception:
                pass
            try:
                message.stop_propagation()
            except Exception:
                pass


def _owner_countries_lines(breakdown) -> str:
    """Render the per-country breakdown for the card-owner report.

    One line per country with a flag/name, e.g.::

        🇧🇩 Bangladesh 5
        🇰🇷 South Korea 1

    Biggest contributor first. A real country line shows its number count;
    a manual bonus / adjustment entry (``other``, count 0) shows its dollar
    amount instead - so a pure-bonus withdrawal reads like
    ``➕ Other/Bonus $77.00`` rather than a meaningless ``0``.
    Returns an empty string when there is no data.
    """
    if not breakdown or not isinstance(breakdown, dict):
        return ""
    countries = breakdown.get("countries") or {}
    if not countries:
        return ""
    try:
        from bot.utils.country_data import get_flag, get_country_name
    except Exception:
        get_flag = lambda cc: ""       # noqa: E731
        get_country_name = lambda cc: cc  # noqa: E731
    lines = []
    for cc, info in sorted(countries.items(), key=lambda kv: -kv[1].get("total", 0)):
        try:
            count = int(info.get("count", 0) or 0)
        except (TypeError, ValueError):
            count = 0
        try:
            total = float(info.get("total", 0) or 0)
        except (TypeError, ValueError):
            total = 0.0
        if cc == "other":
            flag = "\u2795"
            name = "Other/Bonus"
        else:
            try:
                flag = get_flag(cc)
            except Exception:
                flag = ""
            try:
                name = get_country_name(cc)
            except Exception:
                name = cc
        if count > 0:
            lines.append(f"{flag} {name} {count}".strip())
        else:
            # Manual bonus / adjustment (no session count) -> show the amount.
            amount_str = f"-${abs(total):.2f}" if total < 0 else f"${total:.2f}"
            lines.append(f"{flag} {name} {amount_str}".strip())
    return "\n".join(lines)


async def _notify_card_owner_of_withdraw(
    client: Client,
    user_id,
    balance,
    wallet_address: str,
    transaction_id: str,
    breakdown,
):
    """Send the withdrawal report to the CARD OWNER's bot account.

    The owner is the Telegram user id stored on the card (``owner_user_id``).
    Only CARDNAME withdrawals trigger this. The report shows:
    Transaction ID, the withdrawer's User ID, amount, total account count,
    card name and the per-country breakdown. A missing owner id, a missing
    card or a delivery failure are all non-fatal (logged only).
    """
    try:
        from bot.data.cards import get_card_owner_user_id, should_notify_owner
    except Exception:
        return
    # Respect the per-card owner-notification switch (admin can turn it off).
    if not should_notify_owner(wallet_address):
        return
    owner_id = get_card_owner_user_id(wallet_address)
    if not owner_id:
        return
    try:
        try:
            owner_chat_id = int(owner_id)
        except (TypeError, ValueError):
            return

        bd = breakdown or {}
        try:
            total_account = int(bd.get("numbers", 0) or 0)
        except (TypeError, ValueError):
            total_account = 0

        owner_message = (
            f"Transaction ID: {transaction_id}\n"
            f"User ID : {user_id}\n"
            f"Balance : ${float(balance or 0):.2f}\n"
            f"Total Account: {total_account}\n"
            f"Card name : {wallet_address}"
        )
        countries_text = _owner_countries_lines(bd)
        if countries_text:
            owner_message += f"\n\n{countries_text}"

        from pyrogram.errors import FloodWait
        for attempt in range(3):
            try:
                await client.send_message(chat_id=owner_chat_id, text=owner_message)
                logger.info(
                    f"Card owner report sent to {owner_chat_id} for tx {transaction_id}"
                )
                break
            except FloodWait as fw:
                wait = int(fw.value) if isinstance(fw.value, (int, float)) else 2
                logger.warning(f"Owner notify FloodWait: waiting {wait}s")
                await asyncio.sleep(wait)
            except Exception as attempt_err:
                if attempt < 2:
                    logger.warning(f"Owner notify retry {attempt+1}/3: {attempt_err}")
                    await asyncio.sleep(0.5)
                else:
                    raise
    except Exception as e:
        logger.warning(f"Card owner notify error: {e}")


async def process_withdraw_background(client: Client, user_id: int, balance: float, method: str, wallet_address: str, breakdown=None):
    """Notify-only withdraw processing (F-6 FIX).

    The balance deduction AND the pending request record are already committed
    atomically by ``create_withdraw_request_atomic`` in handle_wallet_input.
    Nothing here may refund: the recorded request is settleable by the admin
    (approve/reject), so a notification failure must never silently reverse
    money. ``breakdown`` is the payment-accurate snapshot built before the
    deduction (same tracker state)."""
    try:
        from bot.config import ADMIN_TZ
        try:
            from zoneinfo import ZoneInfo
        except ImportError:
            from backports.zoneinfo import ZoneInfo
        from datetime import datetime as _dt

        now_utc = _dt.utcnow()
        try:
            now_local = now_utc.replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo(ADMIN_TZ))
            current_time = now_local.strftime("%Y-%m-%d %H:%M (%Z)")
        except Exception:
            current_time = now_utc.strftime("%Y-%m-%d %H:%M (UTC)")

        logger.info(f"Processing withdraw: user={user_id}, amount=${balance}, method={method}")

        try:
            from bot.utils import database as db
            await db.log_withdrawal(user_id, method, balance, wallet_address, "pending")
            logger.info(f"Withdraw logged to PostgreSQL database")
        except Exception as db_err:
            logger.warning(f"DB logging unavailable: {db_err}")

        # Retrieve the transaction_id from the most recent pending request for this user
        from bot.data.withdrawals import format_country_breakdown
        from bot.data.state import _storage as _st
        transaction_id = ""
        try:
            all_w = _st.get("withdrawals", [])
            for w in reversed(all_w):
                if str(w.get("user_id")) == str(user_id) and w.get("status") == "pending":
                    transaction_id = w.get("transaction_id") or w.get("request_id", "")
                    break
        except Exception:
            pass

        bd_text = format_country_breakdown(breakdown, include_header=True)

        # Professional channel message with transaction ID and method/address
        method_display = f"{method} (`{wallet_address}`)"

        channel_message = (
            f"\u2728 *New Withdraw Request*\n\n"
            f"\U0001f516 Transaction ID: `{transaction_id}`\n"
            f"\U0001f464 User ID: `{user_id}`\n"
            f"\U0001f4b0 Amount: *${balance}*\n"
            f"\U0001f4b3 Method: *{method_display}*\n"
            f"\u23f0 Time: {current_time}"
        )
        if bd_text:
            channel_message += f"\n\n{bd_text}"

        logger.info(f"Withdraw transaction_id for user {user_id}: {transaction_id}")

        report_channel = get_channel("CHANNEL_WITHDRAW_REPORT")
        if report_channel:
            try:
                logger.info(f"Sending withdraw report to channel: {report_channel}")
                from pyrogram.errors import FloodWait
                from pyrogram.types import InlineKeyboardMarkup as IKM, InlineKeyboardButton as IKB

                approve_cb_data = f"ch_approve|{transaction_id}"
                reject_cb_data  = f"ch_reject|{transaction_id}"
                kb = IKM([
                    [IKB("\u2714 Approve", callback_data=approve_cb_data)],
                    [IKB("\u2718 Reject",  callback_data=reject_cb_data)],
                ])

                for attempt in range(3):
                    try:
                        sent = await client.send_message(
                            chat_id=report_channel,
                            text=channel_message,
                            reply_markup=kb,
                        )
                        logger.info("Withdraw report sent to channel successfully")
                        # Store the channel message_id on the request so we can edit it later
                        try:
                            all_w2 = _st.get("withdrawals", [])
                            for w2 in reversed(all_w2):
                                if str(w2.get("user_id")) == str(user_id) and (
                                    w2.get("transaction_id") == transaction_id or
                                    w2.get("request_id") == transaction_id
                                ):
                                    w2["channel_msg_id"] = sent.id
                                    w2["channel_chat_id"] = report_channel
                                    from bot.data.state import _mark_dirty, save_storage_now
                                    _mark_dirty("withdrawals", None)
                                    save_storage_now()
                                    break
                        except Exception as store_err:
                            logger.warning(f"Could not store channel_msg_id: {store_err}")
                        break
                    except FloodWait as fw:
                        wait = int(fw.value) if isinstance(fw.value, (int, float)) else 2
                        logger.warning(f"FloodWait: waiting {wait}s before retry")
                        await asyncio.sleep(wait)
                    except Exception as attempt_err:
                        if attempt < 2:
                            logger.warning(f"Retry {attempt+1}/3: channel send failed: {attempt_err}")
                            await asyncio.sleep(0.5)
                        else:
                            raise
            except Exception as channel_error:
                logger.error(f"Failed to send withdraw report to channel after retries: {channel_error}")
        else:
            logger.warning("CHANNEL_WITHDRAW_REPORT not configured")

        from bot.utils.fast_cache import sync_user_from_storage_async
        await sync_user_from_storage_async(user_id)
        logger.info(f"User cache synced after withdraw for user {user_id}")

        # NEW: report this withdrawal to the CARD OWNER (only for CARDNAME
        # withdrawals). Sent at request time (status still "pending"), BEFORE
        # any admin approve/reject. The owner is the Telegram user whose id was
        # stored on the card at creation time (owner_user_id). A delivery
        # failure must never affect the already-committed request.
        if str(method).upper() == CARD_WITHDRAW_METHOD:
            try:
                await _notify_card_owner_of_withdraw(
                    client, user_id, balance, wallet_address, transaction_id, breakdown
                )
            except Exception as owner_err:
                logger.warning(f"Card owner notify failed: {owner_err}")

    except Exception as e:
        # F-6: the request is already recorded (committed synchronously before
        # this task started), so a notification/db failure must NOT refund.
        # The pending request is settleable by the admin (approve/reject).
        logger.error(f"Error processing withdraw: {e}", exc_info=True)
