"""
Firebase Realtime Database Backend for TPO Bot
=============================================
Provides cloud storage persistence via Firebase Realtime Database.
Allows running the bot seamlessly across multiple devices (Laptop, VPS, PowerShell, etc.)
using a central cloud database configured via .env:

    FIREBASE_DB_URL=https://<your-project>-default-rtdb.firebaseio.com
    FIREBASE_CREDENTIALS_FILE=firebase_credentials.json

If FIREBASE_DB_URL is set, the bot automatically persists all state
(users, countries, pending_sessions, withdrawals, settings, etc.) to Firebase RTDB.
If FIREBASE_DB_URL is unset, the bot falls back to local SQLite.
"""

import os
import sys
import copy
import logging
import threading
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)

_firebase_initialized = False
_firebase_app = None
_firebase_lock = threading.Lock()
_cached_db_url: Optional[str] = None

# In-memory-capped audit log lists that are nonetheless kept DURABLY in the
# cloud. Their in-memory lists are trimmed (LOG_MAX_ENTRIES), so they must be
# append-merged into the cloud as keyed maps instead of being written as a
# plain list (which would overwrite/drop older cloud entries).
_LOG_SYNC_KEYS = ("transaction_logs", "admin_logs", "failed_verifications")


def _log_entry_key(entry: Dict[str, Any], index: int) -> str:
    """Deterministic, collision-resistant key for an audit-log entry.

    Uses the fields that identify an entry (timestamp + user/actor + action or
    reason) so re-syncing the SAME entry maps to the SAME cloud key (a merge,
    never a duplicate), while distinct entries get distinct keys. ``index`` is
    only a last-resort disambiguator for entries that share all identity
    fields, but it is deliberately NOT part of the primary key so that the key
    stays stable across memory-cap re-ordering for normal entries.
    """
    parts = [
        str(entry.get("timestamp") or entry.get("created_at") or ""),
        str(entry.get("user_id") or entry.get("admin_id") or entry.get("actor_id") or ""),
        str(entry.get("action") or entry.get("reason") or entry.get("event") or ""),
        str(entry.get("amount") or ""),
    ]
    base = "|".join(parts)
    # Keep it Firebase-path-safe (no '.', '#', '$', '/', '[', ']').
    safe = base.replace("/", "_").replace(".", "_").replace("#", "_")
    safe = safe.replace("$", "_").replace("[", "_").replace("]", "_")
    if safe.strip("|_ "):
        return safe or f"entry_{index}"
    # Fully empty identity -> fall back to index so it is still stored once.
    return f"entry_{index}"


def get_firebase_db_url() -> Optional[str]:
    """Retrieve the Firebase Realtime Database URL from environment or .env."""
    url = os.getenv("FIREBASE_DB_URL", "").strip()
    return url if url else None


def get_firebase_credentials_path() -> str:
    """Resolve the path to the service account credentials JSON file."""
    path = os.getenv("FIREBASE_CREDENTIALS_FILE", "firebase_credentials.json").strip()
    if os.path.isabs(path) and os.path.exists(path):
        return path

    # Check relative to workspace / root
    workspace_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    candidate = os.path.join(workspace_root, path)
    if os.path.exists(candidate):
        return candidate

    # Check current directory
    if os.path.exists(path):
        return os.path.abspath(path)

    return path


def _in_test_run() -> bool:
    """True when running under pytest (or an explicit safety flag is set).

    Used as a HARD GUARD so that any code path reached from a test can never
    write to the real Firebase cloud. pytest sets ``PYTEST_CURRENT_TEST`` for
    every test item; ``KILO_TEST_MODE=1`` is an explicit opt-in for ad-hoc
    scripts. When this is True, :func:`init_firebase` refuses to connect.
    """
    if os.getenv("PYTEST_CURRENT_TEST"):
        return True
    if os.getenv("KILO_TEST_MODE", "").strip() in ("1", "true", "True"):
        return True
    return "pytest" in sys.modules


def is_firebase_configured() -> bool:
    """Return True if FIREBASE_DB_URL is configured AND we are not in a test.

    The test guard is intentional: a configured FIREBASE_DB_URL while tests run
    previously caused REAL writes to the production cloud (test data overwrote
    live cards/users/withdrawals). Reporting "not configured" inside tests makes
    every sync/load a safe no-op.
    """
    if _in_test_run():
        return False
    return bool(get_firebase_db_url())


def init_firebase() -> bool:
    """Initialize Firebase Admin SDK with credentials and databaseURL.
    
    Thread-safe and idempotent. Automatically reinitializes if FIREBASE_DB_URL changed.

    SAFETY: refuses to initialize while running under tests (see
    :func:`_in_test_run`), so no test can ever reach the real cloud even if
    FIREBASE_DB_URL is set in the environment/.env.
    """
    global _firebase_initialized, _firebase_app, _cached_db_url

    if _in_test_run():
        logger.warning("Firebase init refused: running under a test (write guard).")
        return False

    db_url = get_firebase_db_url()
    if not db_url:
        return False

    with _firebase_lock:
        try:
            import firebase_admin
            from firebase_admin import credentials, db

            # If already initialized with the SAME url, return True
            if _firebase_initialized and _cached_db_url == db_url and _firebase_app:
                return True

            cred_path = get_firebase_credentials_path()
            if not os.path.exists(cred_path):
                logger.error(
                    f"Firebase credentials file not found at: {cred_path}. "
                    f"Please ensure FIREBASE_CREDENTIALS_FILE points to a valid service account JSON."
                )
                return False

            cred = credentials.Certificate(cred_path)

            # If an existing firebase app is initialized, delete it first to handle URL switch
            if _firebase_app is not None:
                try:
                    firebase_admin.delete_app(_firebase_app)
                except Exception:
                    pass
                _firebase_app = None

            # Check if default app exists in firebase_admin
            try:
                existing_default = firebase_admin.get_app()
                if existing_default:
                    firebase_admin.delete_app(existing_default)
            except ValueError:
                pass

            _firebase_app = firebase_admin.initialize_app(cred, {
                "databaseURL": db_url
            })
            _cached_db_url = db_url

            # Test shallow connection
            ref = db.reference("/")
            _ = ref.get(shallow=True)

            _firebase_initialized = True
            logger.info(f"Firebase Realtime Database initialized successfully at: {db_url}")
            return True

        except Exception as e:
            logger.error(f"Failed to initialize Firebase Realtime Database: {e}")
            _firebase_initialized = False
            return False


def firebase_load_all() -> Optional[Dict[str, Any]]:
    """Load full storage state from Firebase Realtime Database.
    
    Returns structured dictionary matching the bot's _storage schema.
    Returns None on failure so the caller can fall back to local snapshots.

    SAFETY (data-loss guard): a falsy read (``None`` / ``{}``) is treated as a
    FAILED/no-data read and returns ``None``, so the caller falls back to
    SQLite / the local fallback snapshot instead of trusting an empty cloud and
    later overwriting it. An empty Firebase is only accepted as "fresh" when the
    operator explicitly sets ``FIREBASE_ALLOW_EMPTY_INIT=1`` (first-ever
    deploy), which guarantees a transient empty read can never masquerade as a
    brand-new project and wipe real data.
    """
    if not init_firebase():
        return None

    try:
        from firebase_admin import db
        ref = db.reference("/")
        root_data = ref.get()

        data: Dict[str, Any] = {
            "users": {},
            "countries": {},
            "withdrawals": [],
            "admin_logs": [],
            "failed_verifications": [],
            "transaction_logs": [],
            "pending_sessions": {},
            "daily_counters": {},
            "daily_success_counters": {},
            "session_tracker": {},
            "successful_numbers": {},
            "maintenance": False,
            "required_channel": "",
            "withdraw_sessions": {},
        }

        if not root_data or not isinstance(root_data, dict):
            # Empty or non-dict read. Do NOT claim this is a fresh project:
            # treat it as "no data" so the caller falls back to local storage.
            # Only an EXPLICIT opt-in accepts a truly empty cloud as the
            # initial state (first-ever deploy).
            if os.getenv("FIREBASE_ALLOW_EMPTY_INIT", "").strip() in ("1", "true", "True"):
                logger.info(
                    "Firebase Realtime Database is empty and FIREBASE_ALLOW_EMPTY_INIT is set; "
                    "initializing with default state."
                )
                return data
            logger.warning(
                "Firebase returned empty/no data - treating as a failed read and "
                "falling back to local storage (set FIREBASE_ALLOW_EMPTY_INIT=1 to "
                "accept an intentionally empty cloud)."
            )
            return None

        # 1. users
        users = root_data.get("users")
        if isinstance(users, dict):
            for uid, udata in users.items():
                if isinstance(udata, dict):
                    data["users"][str(uid)] = udata

        # 2. countries
        countries = root_data.get("countries")
        if isinstance(countries, dict):
            for cc, cdata in countries.items():
                if isinstance(cdata, dict):
                    data["countries"][str(cc)] = cdata

        # 3. pending_sessions
        pending = root_data.get("pending_sessions")
        if isinstance(pending, dict):
            for sid, pdata in pending.items():
                if isinstance(pdata, dict):
                    data["pending_sessions"][str(sid)] = pdata

        # 4. withdrawals
        # Cloud stores withdrawals as a keyed map (transaction_id -> record) so
        # the node is append-merged and never loses older entries. Legacy clouds
        # may still hold a plain list; both shapes are accepted. The full set is
        # loaded into memory (the in-memory cap trims the VIEW, never the cloud).
        withdrawals = root_data.get("withdrawals")
        if isinstance(withdrawals, list):
            data["withdrawals"] = [w for w in withdrawals if isinstance(w, dict)]
        elif isinstance(withdrawals, dict):
            data["withdrawals"] = [w for w in withdrawals.values() if isinstance(w, dict)]
        # Keep newest-last by timestamp so the in-memory view order is stable.
        try:
            data["withdrawals"].sort(key=lambda w: str(w.get("timestamp") or w.get("created_at") or ""))
        except Exception:
            pass

        # 5. session_tracker
        tracker = root_data.get("session_tracker")
        if isinstance(tracker, dict):
            for uid, tlist in tracker.items():
                if isinstance(tlist, list):
                    data["session_tracker"][str(uid)] = tlist
                elif isinstance(tlist, dict):
                    data["session_tracker"][str(uid)] = list(tlist.values())

        # 6. successful_numbers
        success_nums = root_data.get("successful_numbers")
        if isinstance(success_nums, dict):
            for phone, ndata in success_nums.items():
                if isinstance(ndata, dict):
                    data["successful_numbers"][str(phone)] = ndata

        # 7. settings and general keys
        settings = root_data.get("settings")
        if isinstance(settings, dict):
            for key, val in settings.items():
                data[key] = val

        # Root top-level keys that may have been placed outside settings
        for key in ("daily_counters", "daily_success_counters", "maintenance", "required_channel",
                    "withdraw_sessions"):
            if key in root_data and root_data[key] is not None:
                data[key] = root_data[key]

        # Audit logs are stored in the cloud as keyed maps (append-merged so the
        # in-memory cap can never drop history). Rebuild the ordered list here.
        for key in _LOG_SYNC_KEYS:
            node = root_data.get(key)
            if node is None:
                # Legacy: may still live inside /settings as a plain list.
                node = (root_data.get("settings") or {}).get(key)
            entries = []
            if isinstance(node, dict):
                entries = [v for v in node.values() if isinstance(v, dict)]
            elif isinstance(node, list):
                entries = [v for v in node if isinstance(v, dict)]
            try:
                entries.sort(key=lambda e: str(e.get("timestamp") or e.get("created_at") or ""))
            except Exception:
                pass
            if entries:
                data[key] = entries

        logger.info(
            f"Loaded from Firebase: {len(data['users'])} users, "
            f"{len(data['countries'])} countries, "
            f"{len(data['pending_sessions'])} pending sessions."
        )
        return data

    except Exception as e:
        logger.error(f"Error loading data from Firebase Realtime Database: {e}")
        return None


def firebase_sync_dirty(dirty: Dict[str, Any], storage_snapshot: Dict[str, Any]) -> bool:
    """Incrementally synchronize dirty storage rows to Firebase Realtime Database.
    
    Uses atomic multi-location update (single HTTP request) for all changed paths.
    """
    if not init_firebase():
        return False

    try:
        from firebase_admin import db
        updates: Dict[str, Any] = {}

        users_dirty = dirty.get("users")
        pending_dirty = dirty.get("pending_sessions")
        countries_dirty = dirty.get("countries")
        settings_dirty = dirty.get("settings")
        session_tracker_dirty = dirty.get("session_tracker")
        successful_numbers_dirty = dirty.get("successful_numbers")
        withdrawals_dirty = dirty.get("withdrawals")

        # Users
        if users_dirty is not None:
            users_map = storage_snapshot.get("users", {})
            for uid in users_dirty:
                u_str = str(uid)
                val = users_map.get(u_str)
                updates[f"/users/{u_str}"] = val if val is not None else None

        # Countries
        if countries_dirty is not None:
            c_map = storage_snapshot.get("countries", {})
            for cc in countries_dirty:
                c_str = str(cc)
                val = c_map.get(c_str)
                updates[f"/countries/{c_str}"] = val if val is not None else None

        # Pending sessions
        if pending_dirty is not None:
            p_map = storage_snapshot.get("pending_sessions", {})
            for sid in pending_dirty:
                s_str = str(sid)
                val = p_map.get(s_str)
                updates[f"/pending_sessions/{s_str}"] = val if val is not None else None

        # Successful numbers
        if successful_numbers_dirty is not None:
            s_nums = storage_snapshot.get("successful_numbers", {})
            for phone in successful_numbers_dirty:
                p_str = str(phone)
                val = s_nums.get(p_str)
                updates[f"/successful_numbers/{p_str}"] = val if val is not None else None

        # Session tracker
        # A per-uid map. When the WHOLE table is dirty (``None`` - the prune path,
        # which REMOVES uids) we REPLACE the /session_tracker node with the
        # current in-memory map via ``set()`` at the end, so pruned uids vanish
        # from the cloud too (a merge-only ``update()`` could never delete them).
        # A non-empty set pushes just those uids (a ``None`` value deletes that
        # child). An empty set is the only "unchanged" case.
        _replace_session_tracker = False
        if session_tracker_dirty is None:
            _replace_session_tracker = True
        elif session_tracker_dirty:
            t_map = storage_snapshot.get("session_tracker", {})
            for uid in session_tracker_dirty:
                u_str = str(uid)
                val = t_map.get(u_str)
                updates[f"/session_tracker/{u_str}"] = val if val is not None else None

        # Withdrawals
        # APPEND-ONLY (durable): the in-memory list is capped
        # (WITHDRAWALS_MAX_ENTRIES), so a full-list replace would DELETE older
        # withdrawals from the cloud the moment memory pruned them. Each row is
        # written to its OWN child path (``/withdrawals/<transaction_id>``) so a
        # multi-path update MERGES it and can never remove the other cloud rows.
        #
        # NOTE: ``withdrawals`` is marked as the WHOLE table (value ``None``) by
        # its mutators; ``None`` means "push every in-memory row" and a
        # non-empty set means "push those rows". An empty set means "unchanged"
        # and is the only case skipped.
        if withdrawals_dirty is None or withdrawals_dirty:
            for w in storage_snapshot.get("withdrawals", []) or []:
                if not isinstance(w, dict):
                    continue
                key = str(w.get("transaction_id") or w.get("request_id") or "")
                if key and (withdrawals_dirty is None or key in withdrawals_dirty):
                    updates[f"/withdrawals/{key}"] = w

        # Settings
        if settings_dirty is not None:
            for key in settings_dirty:
                if key not in storage_snapshot:
                    continue
                val = storage_snapshot.get(key)
                if key in _LOG_SYNC_KEYS:
                    # Audit logs are append-merged as per-entry child paths
                    # (``/<log>/<entry_key>``) at their own top-level node (NOT
                    # written into /settings as a plain list, which would
                    # overwrite/drop older cloud entries once the in-memory list
                    # was capped). Per-entry paths guarantee a merge.
                    if isinstance(val, list):
                        for _idx, _e in enumerate(val):
                            if isinstance(_e, dict):
                                updates[f"/{key}/{_log_entry_key(_e, _idx)}"] = _e
                    continue
                updates[f"/settings/{key}"] = val if val is not None else None

        if updates:
            db.reference("/").update(updates)
            logger.debug(f"Firebase synced {len(updates)} dirty path updates")

        # Whole-table session_tracker change (prune): replace the node so removed
        # uids are deleted from the cloud (update() alone only merges/upserts).
        if _replace_session_tracker:
            db.reference("/session_tracker").set(
                storage_snapshot.get("session_tracker", {}) or {}
            )
            logger.debug("Firebase replaced /session_tracker (prune)")

        return True

    except Exception as e:
        logger.error(f"Firebase sync dirty failed: {e}")
        return False


def firebase_sync_all(storage_snapshot: Dict[str, Any]) -> bool:
    """Full synchronization of in-memory storage to Firebase Realtime Database.

    SAFETY (data-loss guard): if the snapshot's syncable tables are ALL empty we
    refuse the full-root overwrite (which in Firebase would DELETE every user /
    country / session node). That situation only occurs when in-memory state is
    empty/untrusted, and overwriting the cloud then is never correct - the
    caller records the failure and keeps local storage authoritative. One
    explicit opt-in (``FIREBASE_ALLOW_EMPTY_INIT=1``) permits a genuine
    first-ever bootstrap of an empty cloud.
    """
    if not init_firebase():
        return False

    try:
        from firebase_admin import db
        table_keys = {
            "users", "countries", "pending_sessions", "withdrawals",
            "session_tracker", "successful_numbers"
        }

        all_empty = not any(
            storage_snapshot.get(k) for k in table_keys
        )
        if all_empty and os.getenv("FIREBASE_ALLOW_EMPTY_INIT", "").strip() not in ("1", "true", "True"):
            logger.error(
                "Refusing full Firebase sync: in-memory tables are all empty. "
                "Overwriting the cloud root now would DELETE existing data. "
                "Set FIREBASE_ALLOW_EMPTY_INIT=1 only for a first-ever bootstrap."
            )
            return False

        settings_dict = {}
        for k, v in storage_snapshot.items():
            if k not in table_keys and not k.startswith("_"):
                settings_dict[k] = v

        payload = {
            "users": storage_snapshot.get("users", {}),
            "countries": storage_snapshot.get("countries", {}),
            "pending_sessions": storage_snapshot.get("pending_sessions", {}),
            "session_tracker": storage_snapshot.get("session_tracker", {}),
            "successful_numbers": storage_snapshot.get("successful_numbers", {}),
            "settings": settings_dict,
        }

        # Withdrawals are written PER CHILD PATH (``/withdrawals/<tx>``) so the
        # cloud node is append-MERGED, never replaced. The in-memory list is
        # capped (WITHDRAWALS_MAX_ENTRIES); sending a whole-node list/map would
        # risk dropping older withdrawals from the cloud once RAM pruned them.
        # The cloud keeps the full history; only the in-memory view is bounded.
        for w in storage_snapshot.get("withdrawals", []) or []:
            if not isinstance(w, dict):
                continue
            _wk = str(w.get("transaction_id") or w.get("request_id") or "")
            if _wk:
                payload[f"withdrawals/{_wk}"] = w

        # Audit logs (transaction_logs / admin_logs / failed_verifications) are
        # ALSO pushed per child path (append-merged) for the same reason: their
        # in-memory lists are capped (LOG_MAX_ENTRIES). They are removed from
        # ``settings_dict`` so the capped list can never overwrite the cloud
        # history; the load path re-merges the maps back into the log lists.
        for _lk in _LOG_SYNC_KEYS:
            entries = storage_snapshot.get(_lk)
            settings_dict.pop(_lk, None)
            if not isinstance(entries, list):
                continue
            for _idx, _e in enumerate(entries):
                if isinstance(_e, dict):
                    payload[f"{_lk}/{_log_entry_key(_e, _idx)}"] = _e

        db.reference("/").update(payload)
        logger.info("Firebase full sync completed successfully.")
        return True

    except Exception as e:
        logger.error(f"Firebase full sync failed: {e}")
        return False
