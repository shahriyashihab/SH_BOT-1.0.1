"""bot/handlers/channel_withdraw_callbacks.py

Handles Approve / Reject inline buttons sent to the withdraw-report channel.

Flow:
  1. Admin taps ✔ Approve or ✘ Reject on the channel message.
  2. Bot shows a confirmation alert ("Are you sure?").
  3. If the admin clicks the Confirm button (second press on same button with
     the confirmation flag), the request is processed and the original channel
     message is edited to show the final status.  No duplicate messages.

Callback-data format:
  ch_approve|<transaction_id>          – first press  → show confirmation
  ch_approve_confirm|<transaction_id>  – second press → execute approve
  ch_reject|<transaction_id>           – first press  → show confirmation
  ch_reject_confirm|<transaction_id>   – second press → execute reject
"""
import logging
import asyncio
from datetime import datetime

from pyrogram.client import Client
from pyrogram import filters
from pyrogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import MessageNotModified, FloodWait

from bot.config import ADMIN_TZ
from bot.utils.storage import approve_withdraw_request, reject_withdraw_request

logger = logging.getLogger(__name__)


async def _reject_non_admin(query: CallbackQuery) -> bool:
    """Authorization gate for the withdraw channel's money buttons.

    These Approve/Reject buttons move REAL MONEY (payout approval / balance
    refund). Before this fix anyone who could press them (e.g. any subscriber
    if the report channel was ever left non-private) could approve a payout.
    Every press now goes through the SAME multi-admin check the admin panel
    uses (primary ADMIN_ID + admins list, int-normalized both sides).

    FAIL-CLOSED on money: any error while deciding (missing from_user, import
    problem, storage hiccup) DENIES the press and logs loudly - it never
    falls through to a payout.
    """
    try:
        user = getattr(query, "from_user", None)
        if user is None:
            raise ValueError("callback has no from_user")
        from bot.handlers.admin.router import is_admin
        if is_admin(user.id):
            return False
        logger.warning(
            f"Non-admin pressed withdraw channel button: user_id={user.id} "
            f"data={getattr(query, 'data', '')!r}"
        )
    except Exception as gate_err:
        logger.error(f"Withdraw channel admin gate error (denying press): {gate_err}", exc_info=True)
    try:
        await query.answer("✘ Only admins can use this button.", show_alert=True)
    except Exception:
        pass
    return True


def _local_time() -> str:
    """Return current time in ADMIN_TZ as a formatted string."""
    try:
        try:
            from zoneinfo import ZoneInfo
        except ImportError:
            from backports.zoneinfo import ZoneInfo
        from datetime import datetime as _dt
        now = _dt.utcnow().replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo(ADMIN_TZ))
        return now.strftime("%Y-%m-%d %H:%M (%Z)")
    except Exception:
        return datetime.utcnow().strftime("%Y-%m-%d %H:%M (UTC)")


def register_channel_withdraw_callbacks(app: Client):
    """Register channel-based approve/reject callback handlers."""

    # ------------------------------------------------------------------ #
    #  FIRST PRESS – show confirmation alert                               #
    # ------------------------------------------------------------------ #

    @app.on_callback_query(filters.regex(r"^ch_approve\|(.+)$"))
    async def ch_approve_first(client: Client, query: CallbackQuery):
        """First tap on Approve – show confirmation pop-up."""
        try:
            if await _reject_non_admin(query):
                return
            tx_id = query.matches[0].group(1)
            await query.answer(
                "⚠️ Are you sure you want to APPROVE this withdrawal?",
                show_alert=True,
            )
            # Replace the keyboard with Confirm / Cancel
            confirm_kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("✔ YES – Confirm Approve", callback_data=f"ch_approve_confirm|{tx_id}")],
                [InlineKeyboardButton("🔙 Cancel", callback_data=f"ch_cancel|{tx_id}")],
            ])
            try:
                await query.message.edit_reply_markup(confirm_kb)
            except (MessageNotModified, Exception):
                pass
        except Exception as e:
            logger.error(f"ch_approve_first error: {e}", exc_info=True)

    @app.on_callback_query(filters.regex(r"^ch_reject\|(.+)$"))
    async def ch_reject_first(client: Client, query: CallbackQuery):
        """First tap on Reject – show confirmation pop-up."""
        try:
            if await _reject_non_admin(query):
                return
            tx_id = query.matches[0].group(1)
            await query.answer(
                "⚠️ Are you sure you want to REJECT this withdrawal? (Balance will be refunded)",
                show_alert=True,
            )
            confirm_kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("✘ YES – Confirm Reject", callback_data=f"ch_reject_confirm|{tx_id}")],
                [InlineKeyboardButton("🔙 Cancel", callback_data=f"ch_cancel|{tx_id}")],
            ])
            try:
                await query.message.edit_reply_markup(confirm_kb)
            except (MessageNotModified, Exception):
                pass
        except Exception as e:
            logger.error(f"ch_reject_first error: {e}", exc_info=True)

    # ------------------------------------------------------------------ #
    #  CANCEL – restore original Approve/Reject keyboard                  #
    # ------------------------------------------------------------------ #

    @app.on_callback_query(filters.regex(r"^ch_cancel\|(.+)$"))
    async def ch_cancel(client: Client, query: CallbackQuery):
        try:
            if await _reject_non_admin(query):
                return
            tx_id = query.matches[0].group(1)
            await query.answer("Cancelled.")
            original_kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("✔ Approve", callback_data=f"ch_approve|{tx_id}")],
                [InlineKeyboardButton("✘ Reject",  callback_data=f"ch_reject|{tx_id}")],
            ])
            try:
                await query.message.edit_reply_markup(original_kb)
            except (MessageNotModified, Exception):
                pass
        except Exception as e:
            logger.error(f"ch_cancel error: {e}", exc_info=True)

    # ------------------------------------------------------------------ #
    #  CONFIRMED APPROVE                                                   #
    # ------------------------------------------------------------------ #

    @app.on_callback_query(filters.regex(r"^ch_approve_confirm\|(.+)$"))
    async def ch_approve_confirmed(client: Client, query: CallbackQuery):
        """Admin confirmed Approve – process and edit the original message."""
        try:
            if await _reject_non_admin(query):
                return
            tx_id = query.matches[0].group(1)
            await query.answer("Processing…")

            result = approve_withdraw_request(tx_id)

            if result.get("success"):
                user_id  = result.get("user_id", "N/A")
                amount   = float(result.get("amount") or 0)
                method   = result.get("method", "N/A")
                wallet   = result.get("wallet_address", "N/A")
                ts_orig  = result.get("timestamp", "")
                breakdown = result.get("country_breakdown")

                from bot.data.withdrawals import format_country_breakdown
                bd_text = format_country_breakdown(breakdown, include_header=True)

                try:
                    from bot.utils.fast_cache import invalidate_user_cache
                    invalidate_user_cache(int(user_id))
                except Exception:
                    pass

                # NOTE: the user is intentionally NOT notified of the decision.
                # The channel report below is the operational record; the user
                # only sees the request-submitted message.

                edit_text = (
                    f"✔ *APPROVED*\n\n"
                    f"🔖 Transaction ID: `{tx_id}`\n"
                    f"👤 User ID: `{user_id}`\n"
                    f"💰 Amount: *${amount:.2f}*\n"
                    f"💳 Method: *{method}* (`{wallet}`)\n"
                    f"📅 Submitted: {ts_orig}\n"
                    f"✔ Approved at: {_local_time()}"
                )
                if bd_text:
                    edit_text += f"\n\n{bd_text}"

                try:
                    await query.message.edit_text(edit_text, reply_markup=None)
                except (MessageNotModified, Exception) as edit_err:
                    logger.warning(f"Edit after approve failed: {edit_err}")

            else:
                err = result.get("error", "Unknown error")
                if err == "Request not found":
                    await query.answer("⚠️ Already settled or not found.", show_alert=True)
                else:
                    await query.answer(f"Error: {err}", show_alert=True)

        except Exception as e:
            logger.error(f"ch_approve_confirmed error: {e}", exc_info=True)
            try:
                await query.answer("Internal error – check logs.", show_alert=True)
            except Exception:
                pass

    # ------------------------------------------------------------------ #
    #  CONFIRMED REJECT                                                    #
    # ------------------------------------------------------------------ #

    @app.on_callback_query(filters.regex(r"^ch_reject_confirm\|(.+)$"))
    async def ch_reject_confirmed(client: Client, query: CallbackQuery):
        """Admin confirmed Reject – process and edit the original message."""
        try:
            if await _reject_non_admin(query):
                return
            tx_id = query.matches[0].group(1)
            await query.answer("Processing…")

            result = reject_withdraw_request(tx_id)

            if result.get("success"):
                user_id  = result.get("user_id", "N/A")
                amount   = float(result.get("amount") or 0)
                method   = result.get("method", "N/A")
                wallet   = result.get("wallet_address", "N/A")
                ts_orig  = result.get("timestamp", "")
                breakdown = result.get("country_breakdown")

                from bot.data.withdrawals import format_country_breakdown
                bd_text = format_country_breakdown(breakdown, include_header=True)

                try:
                    from bot.utils.fast_cache import invalidate_user_cache
                    invalidate_user_cache(int(user_id))
                except Exception:
                    pass

                # NOTE: the user is intentionally NOT notified of the decision.

                edit_text = (
                    f"✘ *REJECTED*\n\n"
                    f"🔖 Transaction ID: `{tx_id}`\n"
                    f"👤 User ID: `{user_id}`\n"
                    f"💰 Amount: *${amount:.2f}*\n"
                    f"💳 Method: *{method}* (`{wallet}`)\n"
                    f"🔄 Balance Refunded: *${amount:.2f}*\n"
                    f"📅 Submitted: {ts_orig}\n"
                    f"✘ Rejected at: {_local_time()}"
                )
                if bd_text:
                    edit_text += f"\n\n{bd_text}"

                try:
                    await query.message.edit_text(edit_text, reply_markup=None)
                except (MessageNotModified, Exception) as edit_err:
                    logger.warning(f"Edit after reject failed: {edit_err}")

            else:
                err = result.get("error", "Unknown error")
                if err == "Request not found":
                    await query.answer("⚠️ Already settled or not found.", show_alert=True)
                else:
                    await query.answer(f"Error: {err}", show_alert=True)

        except Exception as e:
            logger.error(f"ch_reject_confirmed error: {e}", exc_info=True)
            try:
                await query.answer("Internal error – check logs.", show_alert=True)
            except Exception:
                pass
