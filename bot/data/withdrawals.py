"""Refactored from bot.utils.storage: Withdrawal request and config storage operations."""
import random
import time
import secrets
import string

from bot.data.state import (
    Any, Dict, List, Optional, _do_save_background, _mark_dirty,
    _mark_settings_key_dirty, _storage, _storage_lock, datetime, save_storage,
    save_storage_now, thread_safe,
)

# H4: the in-memory withdrawals list used to grow FOREVER (only viewed as
# [-50:]) and was full-replaced on EVERY sync. It is now bounded: every
# PENDING request is always kept (money-critical: approve/reject must find
# it), and the newest terminal history entries beyond this cap are dropped
# from the sync store. The durable ledger already lives in the async
# ``withdrawal_logs`` table (90-day retention, written on every request), so
# pruning only ever trims what the admin views would show.
WITHDRAWALS_MAX_ENTRIES = int(__import__("os").environ.get("WITHDRAWALS_MAX_ENTRIES", "2000") or 2000)


def _prune_withdraw_history_locked() -> int:
    """Drop the OLDEST settled entries once the list exceeds its cap.

    MUST hold _storage_lock. Pending/processing requests are NEVER dropped.
    Returns the number of entries removed (0 when under the cap).
    """
    lst = _storage.get("withdrawals")
    if not isinstance(lst, list) or len(lst) <= WITHDRAWALS_MAX_ENTRIES:
        return 0
    excess = len(lst) - WITHDRAWALS_MAX_ENTRIES
    kept = []
    for w in lst:
        if excess > 0 and (not isinstance(w, dict) or w.get("status") != "pending"):
            excess -= 1
            continue
        kept.append(w)
    _storage["withdrawals"] = kept
    return len(lst) - len(kept)


def _make_transaction_id() -> str:
    """Return a 13-character uppercase alphanumeric transaction ID.

    Uses secrets.choice for cryptographic randomness so two simultaneous
    requests never collide even on a busy server (40^13 = ~549 trillion
    combinations). All characters are uppercase A-Z and digits 0-9,
    matching the format W8GN481BAUD54 shown by the admin.
    """
    chars = string.ascii_uppercase + string.digits
    return ''.join(secrets.choice(chars) for _ in range(13))


def _make_request_id() -> str:
    """Return a short, unique, time-ordered request id (legacy – kept for
    backward-compat with existing pending rows that still carry request_id).

    New code always uses ``transaction_id`` instead.
    """
    return f"w{int(time.time() * 1000)}{random.randint(10000, 99999)}"

def try_deduct_balance_for_withdraw(user_id, amount: float) -> bool:
    """
    Atomically check balance and deduct for withdrawal.
    Prevents double-spend by doing check-and-deduct in one locked operation.

    P0-3: the check-and-deduct previously ran WITHOUT ``_storage_lock`` - two
    concurrent withdraw requests (or a withdraw racing a credit) could both
    pass the balance check and deduct twice (double-spend). The whole
    check-and-deduct now happens inside the same lock domain every other
    money path uses.

    Returns:
        True if balance was sufficient and deducted, False otherwise
    """
    user_id = str(user_id)
    with _storage_lock:
        if user_id not in _storage["users"]:
            return False

        try:
            current_balance = float(_storage["users"][user_id].get("balance") or 0)
        except (TypeError, ValueError):
            current_balance = 0.0
        try:
            amount_f = float(amount)
        except (TypeError, ValueError):
            return False
        if current_balance < amount_f or amount_f <= 0:
            return False

        from bot.utils.money import round_money
        _storage["users"][user_id]["balance"] = round_money(current_balance - amount_f)
        _mark_dirty("users", user_id)

    _do_save_background()

    try:
        from bot.utils.fast_cache import invalidate_user_cache
        invalidate_user_cache(int(user_id))
    except (ImportError, ValueError, TypeError):
        pass

    return True

def record_withdraw_request_only(user_id, method: str, amount: float, wallet_address: str, country_breakdown: Optional[Dict] = None) -> None:
    """Record withdrawal request without deducting balance (balance already deducted atomically).

    Money-critical: persists immediately so a pending withdrawal request is never lost.

    ``country_breakdown`` is the payment-accurate snapshot of which countries'
    numbers produced this balance (per-country count + total of the ACTUAL
    prices each session was credited at). It is stored inside the request so
    the admin panel, approve/reject reports and logs always show the same
    numbers - even if the session tracker changes later.
    """
    tx_id = _make_transaction_id()
    request = {
        "transaction_id": tx_id,
        "request_id": tx_id,          # kept for legacy approve/reject matching
        "user_id": str(user_id),
        "method": method,
        "amount": amount,
        "wallet_address": wallet_address,
        "timestamp": datetime.now().isoformat(),
        "status": "pending"
    }
    if country_breakdown:
        request["country_breakdown"] = country_breakdown
    with _storage_lock:
        _storage["withdrawals"].append(request)
        # H4: bound the list + mark the table so the bounded full-replace
        # sync actually runs (the table is no longer rewritten on every
        # unrelated save).
        _prune_withdraw_history_locked()
        _mark_dirty("withdrawals", None)
    save_storage_now()


def create_withdraw_request_atomic(user_id, method: str, amount: float, wallet_address: str, country_breakdown: Optional[Dict] = None) -> Dict[str, Any]:
    """Deduct the balance AND record the pending request in ONE lock domain.

    F-6 FIX: the old flow deducted the balance synchronously but recorded the
    request inside a spawned background task. A hard kill between the two
    steps left the balance deducted with NO request anywhere - silent user
    money loss. Now the check-and-deduct, the record append and the dirty
    marks all commit inside a single ``_storage_lock`` acquisition and
    persist immediately with ``save_storage_now``: either both exist or
    neither does.

    Returns:
        {"success": True, "request_id": ...} or
        {"success": False, "error": "unknown_user" | "invalid_amount" | "insufficient"}
    """
    user_id = str(user_id)
    try:
        amount_f = float(amount)
    except (TypeError, ValueError):
        return {"success": False, "error": "invalid_amount"}
    if amount_f != amount_f or amount_f == float("inf") or amount_f <= 0:
        return {"success": False, "error": "invalid_amount"}

    result: Dict[str, Any] = {"success": False, "error": "unknown_user"}
    with _storage_lock:
        if user_id not in _storage["users"]:
            return {"success": False, "error": "unknown_user"}
        try:
            current = float(_storage["users"][user_id].get("balance") or 0)
        except (TypeError, ValueError):
            current = 0.0
        if current < amount_f:
            return {"success": False, "error": "insufficient"}

        tx_id = _make_transaction_id()
        request = {
            "transaction_id": tx_id,
            "request_id": tx_id,          # kept for legacy approve/reject matching
            "user_id": user_id,
            "method": method,
            "amount": amount_f,
            "wallet_address": wallet_address,
            "timestamp": datetime.now().isoformat(),
            "status": "pending",
        }
        if country_breakdown:
            request["country_breakdown"] = country_breakdown

        # Commit the money and the record together.
        from bot.utils.money import round_money
        _storage["users"][user_id]["balance"] = round_money(current - amount_f)
        _storage["withdrawals"].append(request)
        _prune_withdraw_history_locked()
        # Consume the (un-withdrawn) tracker sessions that built this balance, so
        # a later withdrawal can never show a country whose balance was already
        # cashed out. Done INSIDE this atomic block: the deduction and the
        # consumption commit together or not at all. The consumed session ids are
        # stored on the request so a later REJECT can un-consume exactly them.
        consumed_ids = _consume_tracker_sessions_locked(user_id)
        if consumed_ids:
            request["consumed_session_ids"] = consumed_ids
        _mark_dirty("users", user_id)
        _mark_dirty("withdrawals", None)
        _mark_dirty("session_tracker", user_id)
        result = {"success": True, "request_id": tx_id, "transaction_id": tx_id}

    save_storage_now()

    try:
        from bot.utils.fast_cache import invalidate_user_cache
        invalidate_user_cache(int(user_id))
    except (ImportError, ValueError, TypeError):
        pass
    return result


def _tracker_entry_token(entry: dict) -> str:
    """Stable identity token for a tracker entry.

    Prefers ``session_id`` (always present in real credits, users.py). Falls back
    to a composite of phone + country + verified_at for a legacy/corrupt entry
    that lost its session_id, so un-consume can still match it precisely.
    Returns "" when no usable identity exists.
    """
    if not isinstance(entry, dict):
        return ""
    sid = entry.get("session_id")
    if sid:
        return "sid:" + str(sid)
    phone = entry.get("phone")
    cc = entry.get("country_code")
    va = entry.get("verified_at")
    if phone or (cc and va):
        return "cmp:%s|%s|%s" % (phone or "", cc or "", va or "")
    return ""


def _consume_tracker_sessions_locked(user_id) -> list:
    """Mark every NOT-yet-withdrawn tracker session of this user as withdrawn.

    MUST hold ``_storage_lock``. Called by ``create_withdraw_request_atomic``
    while it holds that lock, right after the balance is deducted, so the money
    deduction and the session consumption are atomic.

    Why: the withdraw rule is "one withdrawal cashes out the ENTIRE current
    balance, grouped by the countries that produced it". Once that balance is
    taken, those sessions must never be counted again - otherwise the NEXT
    withdrawal keeps showing countries (Bangladesh, Nigeria, ...) whose balance
    was already paid out. Each consumed session keeps its original data and just
    gains ``withdrawn: True`` + ``withdrawn_at`` (audit-friendly, prune-safe).

    Returns the list of entry TOKENS that were consumed, so a later REJECT can
    refund the balance AND un-consume exactly those sessions (a rejected
    withdrawal was never paid out, so its countries must reappear).
    """
    tracker = _storage.get("session_tracker")
    if not isinstance(tracker, dict):
        return []
    sessions = tracker.get(str(user_id))
    if not isinstance(sessions, list):
        return []
    now = datetime.now().isoformat()
    consumed_tokens = []
    for entry in sessions:
        if not isinstance(entry, dict):
            continue
        if entry.get("withdrawn") is True:
            continue
        entry["withdrawn"] = True
        entry["withdrawn_at"] = now
        token = _tracker_entry_token(entry)
        if token:
            consumed_tokens.append(token)
    return consumed_tokens


def _unconsume_tracker_sessions_locked(user_id, tokens) -> int:
    """Reverse ``_consume_tracker_sessions_locked`` for the given entry tokens.

    MUST hold ``_storage_lock``. Used by ``reject_withdraw_request``: a rejected
    withdrawal is refunded, so the sessions it consumed must become withdrawable
    again (their countries must show up in the next withdrawal). Only the exact
    tokens recorded on the rejected request are touched - sessions consumed by
    an EARLIER, already-approved withdrawal are never resurrected.

    Returns the number of sessions un-consumed.
    """
    if not tokens:
        return 0
    wanted = {str(t) for t in tokens}
    tracker = _storage.get("session_tracker")
    if not isinstance(tracker, dict):
        return 0
    sessions = tracker.get(str(user_id))
    if not isinstance(sessions, list):
        return 0
    restored = 0
    for entry in sessions:
        if not isinstance(entry, dict):
            continue
        if entry.get("withdrawn") is not True:
            continue
        if _tracker_entry_token(entry) in wanted:
            entry["withdrawn"] = False
            entry.pop("withdrawn_at", None)
            restored += 1
    return restored


def build_country_breakdown(user_id, expected_total: Optional[float] = None) -> Dict[str, Any]:
    """Build the payment-accurate per-country breakdown for a user.

    Reads the session tracker (every successful session stores the REAL amount
    it was credited at, plus its country code) and groups by country:

        {
          "+880": {"count": 5, "total": 2.50},
          "+82":  {"count": 2, "total": 2.00},
        }

    ONLY sessions that have NOT already been withdrawn are counted. A session
    becomes "withdrawn" the moment its balance is taken by a withdrawal
    (``consume_tracker_for_withdraw``). Without this filter the breakdown showed
    EVERY credited session forever, so a country the user had already cashed
    out kept appearing in later withdrawals even though that balance was gone.
    Legacy entries without the flag are treated as not-yet-withdrawn.

    The grand total equals the sum of every UN-WITHDRAWN credited session, i.e.
    exactly the fresh balance the user built from verified numbers since the
    last withdrawal (no current-price guessing).

    When ``expected_total`` is given (the withdraw amount), any difference
    (manual bonus / adjustment credited via ``add_balance_for_user``) is
    returned as an ``"other"`` entry so the displayed total ALWAYS equals the
    amount actually being withdrawn - payment-critical, no mismatch.

    Returns ``{"countries": {...}, "total": float, "numbers": int}``.
    """
    user_id = str(user_id)
    tracker = _storage.get("session_tracker", {}).get(user_id, [])
    countries = {}
    total = 0.0
    numbers = 0
    for entry in tracker:
        if not isinstance(entry, dict):
            continue
        # Skip sessions whose balance has already been withdrawn. Legacy
        # entries have no flag and are therefore (correctly) still pending.
        if entry.get("withdrawn") is True:
            continue
        try:
            amt = float(entry.get("amount") or 0)
        except (TypeError, ValueError):
            amt = 0.0
        cc = entry.get("country_code") or "other"
        if cc not in countries:
            countries[cc] = {"count": 0, "total": 0.0}
        countries[cc]["count"] += 1
        countries[cc]["total"] = round(countries[cc]["total"] + amt, 4)
        total = round(total + amt, 4)
        numbers += 1

    if expected_total is not None:
        try:
            expected_total = float(expected_total)
        except (TypeError, ValueError):
            expected_total = None
        if expected_total is not None:
            diff = round(expected_total - total, 2)
            # Manual bonus / adjustment above session earnings -> show it as
            # an explicit "Other" line so totals always reconcile.
            if diff > 0.005:
                countries.setdefault("other", {"count": 0, "total": 0.0})
                countries["other"]["total"] = round(countries["other"].get("total", 0.0) + diff, 2)
                total = round(total + diff, 2)
            # Negative difference (refunds / corrections) -> show as a note so
            # the admin sees the true earnings, not a wrong-looking total.
            elif diff < -0.005:
                countries.setdefault("other", {"count": 0, "total": 0.0})
                countries["other"]["total"] = round(countries["other"].get("total", 0.0) + diff, 2)
                total = round(total + diff, 2)

    return {
        "countries": countries,
        "total": round(total, 2),
        "numbers": numbers,
    }


def format_country_breakdown(breakdown: Optional[Dict], include_header: bool = True) -> str:
    """Render the breakdown as a clean, aligned text block.

    Examples:
        🇧🇩 Bangladesh   5 numbers   $2.50
        🇰🇷 South Korea  2 numbers   $2.00
        ────────────────────────────
        Total: 8 numbers = $5.00

    Returns an empty string when there is no breakdown data.
    """
    if not breakdown or not isinstance(breakdown, dict):
        return ""
    countries = breakdown.get("countries") or {}
    total = breakdown.get("total", 0.0)
    numbers = breakdown.get("numbers", 0)
    if not countries:
        return ""

    from bot.utils.country_data import get_flag, get_country_name

    lines = []
    if include_header:
        lines.append(f"🌍 BALANCE SOURCE — {numbers} number{'s' if numbers != 1 else ''}")
        lines.append("────────────────────────────")
    # Sort by total descending (biggest contributor first) - professional.
    for cc, info in sorted(countries.items(), key=lambda kv: -kv[1].get("total", 0)):
        flag = get_flag(cc) if cc != "other" else "➕"
        name = get_country_name(cc) if cc != "other" else "Other/Bonus"
        cnt = info.get("count", 0)
        tot = info.get("total", 0.0)
        amount_str = f"-${abs(tot):.2f}" if tot < 0 else f"${tot:.2f}"
        if cnt > 0:
            number_word = "number" if cnt == 1 else "numbers"
            lines.append(f"{flag} {name:<16} {cnt} {number_word:<7} {amount_str}")
        else:
            # Manual bonus / adjustment line (no session count)
            label = "Other/Bonus" if tot >= 0 else "Adjustment"
            lines.append(f"{flag} {label:<16} {'':9} {amount_str}")
    lines.append("────────────────────────────")
    lines.append(f"✔ Total: {numbers} numbers = ${total:.2f}")
    return "\n".join(lines)

def force_logout_users() -> None:
    """Force logout all users."""
    from bot.data.logs import append_bounded
    append_bounded("admin_logs", {
        "action": "force_logout_all",
        "timestamp": datetime.now().isoformat()
    })
    save_storage()

def save_withdraw_state(user_id: int, data: Dict[str, Any]) -> None:
    """Persist an in-progress withdraw flow so it survives a bot restart.

    Only JSON-serializable fields are stored (balance, step, method, ...).
    Immediate save so the state survives a hard kill mid-flow.
    """
    _storage.setdefault("withdraw_sessions", {})[str(user_id)] = data
    _mark_settings_key_dirty("withdraw_sessions")
    save_storage_now()

def load_withdraw_state(user_id: int) -> Optional[Dict[str, Any]]:
    """Load a persisted in-progress withdraw flow, if any."""
    return _storage.get("withdraw_sessions", {}).get(str(user_id))

def clear_withdraw_state(user_id: int) -> None:
    """Remove a persisted withdraw flow once it completes or is cancelled."""
    sess = _storage.get("withdraw_sessions")
    if sess and str(user_id) in sess:
        sess.pop(str(user_id), None)
        _mark_settings_key_dirty("withdraw_sessions")
        save_storage()

def is_withdraw_enabled() -> bool:
    """Check if withdrawal is enabled."""
    return not _storage.get("withdraw_disabled", False)

def toggle_withdraw() -> bool:
    """Toggle withdraw enabled/disabled."""
    current = _storage.get("withdraw_disabled", False)
    _storage["withdraw_disabled"] = not current
    _mark_settings_key_dirty("withdraw_disabled")
    save_storage()
    return not _storage["withdraw_disabled"]

def set_user_withdraw_config(user_id, min_amt=None, max_amt=None, disabled=None):
    """Set custom withdraw configuration for a specific user."""
    user_id = str(user_id)
    with _storage_lock:
        if user_id not in _storage.get("users", {}):
            return False

        if "withdraw_config" not in _storage["users"][user_id]:
            _storage["users"][user_id]["withdraw_config"] = {}

        if min_amt is not None:
            _storage["users"][user_id]["withdraw_config"]["min"] = float(min_amt)
        if max_amt is not None:
            _storage["users"][user_id]["withdraw_config"]["max"] = float(max_amt)
        if disabled is not None:
            _storage["users"][user_id]["withdraw_config"]["disabled"] = bool(disabled)

        _mark_dirty("users", user_id)
        save_storage()
    try:
        from bot.utils.fast_cache import invalidate_user_cache
        invalidate_user_cache(int(user_id))
    except Exception:
        pass
    return True

def get_user_withdraw_config(user_id) -> Dict[str, Any]:
    """Get custom withdraw configuration for a specific user (a COPY, not live)."""
    user_id = str(user_id)
    with _storage_lock:
        user = _storage.get("users", {}).get(user_id, {})
        cfg = user.get("withdraw_config", {})
        return dict(cfg) if isinstance(cfg, dict) else {}

def get_pending_withdraw_requests() -> List[Dict[str, Any]]:
    """Get pending withdraw requests."""
    return [w for w in _storage.get("withdrawals", []) if w.get("status") == "pending"]

def approve_withdraw_request(index_or_id) -> Dict[str, Any]:
    """Approve a pending withdraw request.

    Accepts either a stable ``request_id`` (preferred - race-free: the id is
    unique per request, so a click always acts on the EXACT request the admin
    saw) or a legacy positional index into the pending list.

    E-4 FIX: the ENTIRE find-and-flip now runs inside ``_storage_lock``.
    Previously the pending-check, status flip and save ran unlocked, so two
    concurrent admin clicks (approve+approve or approve+reject) could BOTH
    observe ``status == "pending"`` - paying AND refunding the same request.
    The status flip is also matched by request_id first (legacy rows fall
    back to the user_id+timestamp pair) and persisted with
    ``save_storage_now`` (a crash inside the old 0.5s debounce window could
    erase the approval and let a later reject refund an ALREADY-PAID payout).

    Args:
        index_or_id: request_id string or index of the pending request

    Returns:
        Dict with request details or error info
    """
    result: Dict[str, Any] = {"success": False, "error": "Request not found"}
    with _storage_lock:
        all_withdrawals = _storage.get("withdrawals", [])
        pending = [w for w in all_withdrawals if w.get("status") == "pending"]

        # Match by stable request_id first (CRITICAL: index-based matching could
        # act on a DIFFERENT request if the pending list changed between the
        # panel render and the admin's click - approving/rejecting the wrong
        # user's withdrawal).
        target_request = None
        if isinstance(index_or_id, str):
            for w in pending:
                # Accept both new 13-char transaction_id and legacy w... request_id
                if w.get("transaction_id") == index_or_id or w.get("request_id") == index_or_id:
                    target_request = w
                    break
        else:
            index = index_or_id
            if index < 0 or index >= len(pending):
                return {"success": False, "error": "Invalid request index"}
            target_request = pending[index]

        if target_request is None:
            return {"success": False, "error": "Request not found"}

        target_id = target_request.get("transaction_id") or target_request.get("request_id")
        for w in all_withdrawals:
            if target_id:
                if w.get("request_id") == target_id and w.get("status") == "pending":
                    w["status"] = "approved"
                    w["approved_at"] = datetime.now().isoformat()
                    # H4: the withdrawals table is no longer rewritten on every save -
                    # mutations MUST mark it or the status flip would never persist.
                    _mark_dirty("withdrawals", None)
                    result = {
                        "success": True,
                        "user_id": w.get("user_id"),
                        "amount": w.get("amount"),
                        "method": w.get("method"),
                        "wallet_address": w.get("wallet_address"),
                        "timestamp": w.get("timestamp", ""),
                        "country_breakdown": w.get("country_breakdown"),
                    }
                    break
            else:
                # Legacy row without a request_id: keep the historical
                # (user_id, timestamp) pair matching for backwards compatibility.
                if (w.get("user_id") == target_request.get("user_id") and
                    w.get("timestamp") == target_request.get("timestamp") and
                    w.get("status") == "pending"):
                    w["status"] = "approved"
                    w["approved_at"] = datetime.now().isoformat()
                    _mark_dirty("withdrawals", None)
                    result = {
                        "success": True,
                        "user_id": w.get("user_id"),
                        "amount": w.get("amount"),
                        "method": w.get("method"),
                        "wallet_address": w.get("wallet_address"),
                        "timestamp": w.get("timestamp", ""),
                        "country_breakdown": w.get("country_breakdown"),
                    }
                    break

    # Persist OUTSIDE the lock (save acquires its own locks; holding
    # _storage_lock across the DB write risks lock-order inversion).
    if result.get("success"):
        save_storage_now()
    return result

def reject_withdraw_request(index_or_id) -> Dict[str, Any]:
    """Reject a pending withdraw request and refund balance.

    Accepts either a stable ``request_id`` (preferred - race-free: the id is
    unique per request, so a click always acts on the EXACT request the admin
    saw) or a legacy positional index into the pending list.

    E-4 FIX: the ENTIRE find-and-flip-and-refund now runs inside
    ``_storage_lock``. Previously the pending-check, status flip and refund
    ran unlocked, so a concurrent approve + reject could BOTH observe
    ``status == "pending"`` - the user received the payout AND the refund.

    Args:
        index_or_id: request_id string or index of the pending request

    Returns:
        Dict with request details or error info
    """
    result: Dict[str, Any] = {"success": False, "error": "Request not found"}
    with _storage_lock:
        all_withdrawals = _storage.get("withdrawals", [])
        pending = [w for w in all_withdrawals if w.get("status") == "pending"]

        # Match by stable request_id first (CRITICAL: index-based matching could
        # refund the WRONG user's balance if the pending list changed between the
        # panel render and the admin's click).
        target_request = None
        if isinstance(index_or_id, str):
            for w in pending:
                # Accept both new 13-char transaction_id and legacy w... request_id
                if w.get("transaction_id") == index_or_id or w.get("request_id") == index_or_id:
                    target_request = w
                    break
        else:
            index = index_or_id
            if index < 0 or index >= len(pending):
                return {"success": False, "error": "Invalid request index"}
            target_request = pending[index]

        if target_request is None:
            return {"success": False, "error": "Request not found"}

        user_id = target_request.get("user_id")
        # Coerce to float once, up-front, so a legacy/corrupt string value
        # ("40.0") can never raise TypeError on the refund arithmetic below.
        # A bad amount falls back to 0.0 and the row is still flipped+refunded
        # (defensive: never leave a request rejected without its refund).
        try:
            amount = float(target_request.get("amount", 0) or 0)
        except (TypeError, ValueError):
            amount = 0.0

        target_id = target_request.get("transaction_id") or target_request.get("request_id")
        for w in all_withdrawals:
            if target_id:
                if w.get("request_id") == target_id and w.get("status") == "pending":
                    w["status"] = "rejected"
                    w["rejected_at"] = datetime.now().isoformat()

                    if user_id in _storage.get("users", {}):
                        try:
                            current_balance = float(_storage["users"][user_id].get("balance", 0) or 0)
                        except (TypeError, ValueError):
                            current_balance = 0.0
                        from bot.utils.money import round_money
                        _storage["users"][user_id]["balance"] = round_money(current_balance + amount)
                        _mark_dirty("users", user_id)

                    # A rejected withdrawal is refunded, so the sessions IT
                    # consumed become withdrawable again (their countries must
                    # show in the next withdrawal). Only the exact ids recorded
                    # on this request are restored.
                    _restored = _unconsume_tracker_sessions_locked(
                        user_id, w.get("consumed_session_ids") or [])
                    if _restored:
                        _mark_dirty("session_tracker", user_id)

                    _mark_dirty("withdrawals", None)
                    result = {
                        "success": True,
                        "user_id": user_id,
                        "amount": amount,
                        "method": w.get("method"),
                        "wallet_address": w.get("wallet_address", ""),
                        "timestamp": w.get("timestamp", ""),
                        "country_breakdown": w.get("country_breakdown"),
                        "refunded": True
                    }
                    break
            else:
                # Legacy row without a request_id: keep the historical
                # (user_id, timestamp) pair matching for backwards compatibility.
                if (w.get("user_id") == user_id and
                    w.get("timestamp") == target_request.get("timestamp") and
                    w.get("status") == "pending"):
                    w["status"] = "rejected"
                    w["rejected_at"] = datetime.now().isoformat()

                    if user_id in _storage.get("users", {}):
                        try:
                            current_balance = float(_storage["users"][user_id].get("balance", 0) or 0)
                        except (TypeError, ValueError):
                            current_balance = 0.0
                        from bot.utils.money import round_money
                        _storage["users"][user_id]["balance"] = round_money(current_balance + amount)
                        _mark_dirty("users", user_id)

                    # See the request_id branch above: refund -> un-consume.
                    _restored = _unconsume_tracker_sessions_locked(
                        user_id, w.get("consumed_session_ids") or [])
                    if _restored:
                        _mark_dirty("session_tracker", user_id)

                    _mark_dirty("withdrawals", None)
                    result = {
                        "success": True,
                        "user_id": user_id,
                        "amount": amount,
                        "method": w.get("method"),
                        "wallet_address": w.get("wallet_address", ""),
                        "timestamp": w.get("timestamp", ""),
                        "country_breakdown": w.get("country_breakdown"),
                        "refunded": True
                    }
                    break

    if result.get("success"):
        save_storage_now()

        try:
            from bot.utils.fast_cache import invalidate_user_cache
            invalidate_user_cache(int(result["user_id"]))
        except (ImportError, ValueError, TypeError):
            pass

    return result

def get_withdraw_logs() -> List[Dict[str, Any]]:
    """Get all withdraw logs."""
    return _storage.get("withdrawals", [])[-50:]

def get_min_withdraw_amounts() -> Dict[str, float]:
    """Get minimum withdraw amounts for each method (a COPY, not live storage)."""
    with _storage_lock:
        stored = _storage.get("min_withdraw")
        if isinstance(stored, dict):
            return dict(stored)
        return {
            "usdt": 10.0,
            "trx": 10.0,
            "cardname": 10.0,
            "binance": 10.0,
        }

def get_min_withdraw_for_method(method: str) -> float:
    """Get minimum withdraw for a specific method."""
    mins = get_min_withdraw_amounts()
    return mins.get(method.lower(), 10.0)

def set_min_withdraw_for_method(method: str, amount: float) -> None:
    """Set minimum withdraw for a specific method."""
    with _storage_lock:
        if "min_withdraw" not in _storage:
            _storage["min_withdraw"] = {
                "usdt": 10.0,
                "trx": 10.0,
                "cardname": 10.0,
                "binance": 10.0
            }
        _storage["min_withdraw"][method.lower()] = amount
        _mark_settings_key_dirty("min_withdraw")
        save_storage()
