from pyrogram.client import Client
from pyrogram import filters
from pyrogram.types import Message
from bot.utils.storage import get_open_countries, get_country_rate, get_country_timeout, get_user_effective_rate, is_user_banned, get_message_template, get_capacity_ui
from bot.utils.country_data import get_flag, get_country_alpha2, get_country_name
from bot.config import ADMIN_ID

CAP_LEGEND = "FREE = Spam Free (good acc) · LIM = Limit acc · SPAM = Spam acc"
CAP_LEGEND_BANNER = "FREE = Clean acc · NEW = Limited acc · SPAM = Spam acc"


def _fmt_price(value) -> str:
    """Format a rate value as a USD price string (None/0 safe)."""
    try:
        return f"${float(value or 0):.2f}"
    except (TypeError, ValueError):
        return "$0.00"


def build_cap_table(user_id, countries):
    """Build the professional, perfectly-aligned /cap table.

    Every line uses fixed-width columns so nothing ever drifts:
        flag(2 cells)  name(2)  code(6)  free(6)  lim(6)  spam

    Rows are sorted by spam-free (effective) price, highest first.
    Returns ``(table_text, best_entry)`` where ``best_entry`` is the
    ``(flag, alpha2, code, free, lim, spam)`` tuple of the top-paying country
    (or ``None`` when the list is empty).
    """
    W = 6
    entries = []
    for cc in countries:
        free = get_user_effective_rate(user_id, cc)
        lim = get_country_rate(cc, "limit")
        sp = get_country_rate(cc, "spam")
        entries.append((get_flag(cc), get_country_alpha2(cc), cc, free, lim, sp))

    entries.sort(key=lambda e: (e[3] if e[3] is not None else 0.0), reverse=True)

    lines = ["COUNTRY" + " " * 6 + "FREE".ljust(W) + " " + "LIM".ljust(W) + " " + "SPAM"]
    for flag, a2, cc, free, lim, sp in entries:
        lines.append(
            f"{flag} {a2} {cc.ljust(W)} {_fmt_price(free).ljust(W)} "
            f"{_fmt_price(lim).ljust(W)} {_fmt_price(sp)}"
        )

    return "\n".join(lines), (entries[0] if entries else None)


def build_cap_banner(user_id, countries):
    """Build the banner-style /cap view.

    Every country is rendered as two identical-shaped lines, so the whole
    list stays perfectly uniform (each limit/price/timeout uses the real
    configured values - nothing is guessed):

        🇦🇴 +244 Angola
        Free:$0.15|New:$0.15|Spam:$0.0| |600s

    Rows are sorted by spam-free (effective) price, highest first.
    Returns ``(banner_text, best_entry)`` where ``best_entry`` is the
    ``(flag, alpha2, code, free, lim, spam)`` tuple of the top-paying country
    (or ``None`` when the list is empty).
    """
    entries = []
    for cc in countries:
        free = get_user_effective_rate(user_id, cc)
        lim = get_country_rate(cc, "limit")
        sp = get_country_rate(cc, "spam")
        timeout = get_country_timeout(cc)
        entries.append((get_flag(cc), get_country_alpha2(cc), cc, get_country_name(cc), free, lim, sp, timeout))

    entries.sort(key=lambda e: (e[4] if e[4] is not None else 0.0), reverse=True)

    blocks = []
    for flag, a2, cc, name, free, lim, sp, timeout in entries:
        blocks.append(
            f"{flag} {cc} {name}\n"
            f"Free:{_fmt_price(free)}|New:{_fmt_price(lim)}|Spam:{_fmt_price(sp)}| |{timeout}s"
        )

    banner = "\n\n".join(blocks)
    best = None
    if entries:
        flag, a2, cc, name, free, lim, sp, timeout = entries[0]
        best = (flag, a2, cc, free, lim, sp)

    return banner, best


def _cap_entries(user_id, countries, with_timeout=False):
    """Shared collector: sorted (flag, a2, cc, name, free, lim, spam, timeout).

    Sorted by spam-free (effective) price, highest first. Used by every
    builder so all UIs agree on the data and the ordering.
    """
    entries = []
    for cc in countries:
        free = get_user_effective_rate(user_id, cc)
        lim = get_country_rate(cc, "limit")
        sp = get_country_rate(cc, "spam")
        timeout = get_country_timeout(cc) if with_timeout else None
        entries.append((get_flag(cc), get_country_alpha2(cc), cc, get_country_name(cc), free, lim, sp, timeout))
    entries.sort(key=lambda e: (e[4] if e[4] is not None else 0.0), reverse=True)
    return entries


def _best_of(entries):
    """Best (flag, a2, cc, free, lim, spam) tuple, or None when empty."""
    if not entries:
        return None
    flag, a2, cc, _name, free, lim, sp, _to = entries[0]
    return (flag, a2, cc, free, lim, sp)


def build_cap_badges(user_id, countries):
    """UI #3 - Status Badges (no timeout).

        🇧🇩 +880 Bangladesh
           Free $1.00   Limit $0.10   Spam $0.10

    Mobile-safe: header line + one indented detail line per country.
    """
    entries = _cap_entries(user_id, countries)
    blocks = []
    for flag, _a2, cc, name, free, lim, sp, _to in entries:
        blocks.append(
            f"{flag} {cc} {name}\n"
            f"   Free {_fmt_price(free)}   Limit {_fmt_price(lim)}   Spam {_fmt_price(sp)}"
        )
    return "\n".join(blocks), _best_of(entries)


def build_cap_tiers(user_id, countries):
    """UI #4 - Tier Groups (with timeout).

        ── PREMIUM (>= $0.50) ──
          🇧🇩 +880 Bangladesh
             Free:$1.00 Lim:$0.10 Spam:$0.10 | 300s

    Mobile-safe: countries grouped by price band, no column alignment.
    """
    entries = _cap_entries(user_id, countries, with_timeout=True)

    def _tier(free):
        try:
            f = float(free or 0)
        except (TypeError, ValueError):
            f = 0.0
        if f >= 0.5:
            return "💎 PREMIUM (>= $0.50)"
        if f >= 0.2:
            return "⭐ STANDARD ($0.20 - $0.50)"
        return "🔹 BASIC (< $0.20)"

    groups = {}
    for e in entries:
        groups.setdefault(_tier(e[4]), []).append(e)

    L = []
    order = ["💎 PREMIUM (>= $0.50)", "⭐ STANDARD ($0.20 - $0.50)", "🔹 BASIC (< $0.20)"]
    for g in order:
        if g not in groups:
            continue
        L.append(g)
        for flag, _a2, cc, name, free, lim, sp, to in groups[g]:
            L.append(
                f"  {flag} {cc} {name}\n"
                f"     Free:{_fmt_price(free)} Lim:{_fmt_price(lim)} Spam:{_fmt_price(sp)} | {to}s"
            )
        L.append("")
    return "\n".join(L).rstrip(), _best_of(entries)


def build_cap_tree(user_id, countries):
    """UI #5 - Tree View (with timeout).

        🇧🇩 +880  Bangladesh (300s)
           Free $1.00
           Limit $0.10
           Spam $0.10

    Mobile-safe: each value on its own line.
    """
    entries = _cap_entries(user_id, countries, with_timeout=True)
    blocks = []
    for flag, _a2, cc, name, free, lim, sp, to in entries:
        blocks.append(
            f"{flag} {cc}  {name} ({to}s)\n"
            f"   Free {_fmt_price(free)}\n"
            f"   Limit {_fmt_price(lim)}\n"
            f"   Spam {_fmt_price(sp)}"
        )
    return "\n".join(blocks), _best_of(entries)


def build_cap_arrow(user_id, countries):
    """UI #6 - Arrow Pointer (no timeout).

        🇧🇩 +880 Bangladesh
           Free $1.00 / Lim $0.10 / Spam $0.10

    Mobile-safe: header + single indented detail line.
    """
    entries = _cap_entries(user_id, countries)
    blocks = []
    for flag, _a2, cc, name, free, lim, sp, _to in entries:
        blocks.append(
            f"{flag} {cc} {name}\n"
            f"   Free {_fmt_price(free)} / Lim {_fmt_price(lim)} / Spam {_fmt_price(sp)}"
        )
    return "\n".join(blocks), _best_of(entries)


def build_cap_card(user_id, countries):
    """UI #7 - Clean Card (with timeout).

        🇧🇩 Bangladesh +880
        free $1.00 | limit $0.10 | spam $0.10 | 300s

    Mobile-safe: name header + one pipe-separated detail line per country.
    """
    entries = _cap_entries(user_id, countries, with_timeout=True)
    blocks = []
    for flag, _a2, cc, name, free, lim, sp, to in entries:
        blocks.append(
            f"{flag} {name} {cc}\n"
            f"free {_fmt_price(free)} | limit {_fmt_price(lim)} | spam {_fmt_price(sp)} | {to}s"
        )
    return "\n".join(blocks), _best_of(entries)


def build_cap_minimal(user_id, countries):
    """UI #8 - Minimal Lines (no timeout).

        🇧🇩 +880 Bangladesh — F$1.00 · L$0.10 · S$0.10

    Mobile-safe: a single short line per country.
    """
    entries = _cap_entries(user_id, countries)
    lines = []
    for flag, _a2, cc, name, free, lim, sp, _to in entries:
        lines.append(
            f"{flag} {cc} {name} — F{_fmt_price(free)} · L{_fmt_price(lim)} · S{_fmt_price(sp)}"
        )
    return "\n".join(lines), _best_of(entries)


CAPACITY_UIS = {
    1: {
        "name": "Classic Table",
        "legend": CAP_LEGEND,
        "builder": build_cap_table,
    },
    2: {
        "name": "Banner View",
        "legend": CAP_LEGEND_BANNER,
        "builder": build_cap_banner,
    },
    3: {
        "name": "Status Badges",
        "legend": "Free / Limit / Spam per account tier",
        "builder": build_cap_badges,
    },
    4: {
        "name": "Tier Groups",
        "legend": "Grouped by spam-free price band",
        "builder": build_cap_tiers,
    },
    5: {
        "name": "Tree View",
        "legend": "Free / Limit / Spam branches per country",
        "builder": build_cap_tree,
    },
    6: {
        "name": "Arrow Pointer",
        "legend": "Free / Lim / Spam per line",
        "builder": build_cap_arrow,
    },
    7: {
        "name": "Clean Card",
        "legend": "One card per country",
        "builder": build_cap_card,
    },
    8: {
        "name": "Minimal Lines",
        "legend": "Compact one-line per country",
        "builder": build_cap_minimal,
    },
}


def build_capacity_response(user_id, countries, total, ui_id=None):
    """Render the /cap reply body for the given (or active) capacity UI.

    ``ui_id`` selects a specific UI; when None the admin-selected UI stored
    in settings is used. Unknown ids fall back to UI #1 (table).
    Returns ``(response_text, legend)``.
    """
    from bot.utils.i18n import get_user_lang, user_t
    if ui_id is None:
        ui_id = get_capacity_ui()
    if ui_id not in CAPACITY_UIS:
        ui_id = 1

    cfg = CAPACITY_UIS[ui_id]
    content, best = cfg["builder"](user_id, countries)

    best_line = ""
    if best:
        flag, _a2, cc, free, _lim, _sp = best
        best_line = user_t("cap_best", user_id, flag=flag, code=cc, price=_fmt_price(free))

    response = (
        f"{user_t('cap_header', user_id)}\n"
        f"{user_t('cap_subtitle', user_id, total=total)}\n"
        f"```\n{content}\n```\n"
        f"{cfg['legend']}\n"
        f"{user_t('cap_total', user_id, total=total)}\n"
        f"{best_line}"
    )
    return response


def register_cap_handler(app: Client):
    @app.on_message(filters.command("cap"))
    async def show_capacity(client: Client, message: Message):
        user_id = message.chat.id
        from bot.utils.i18n import get_user_lang, user_t
        lang = get_user_lang(user_id)

        if is_user_banned(user_id):
            await message.reply_text(get_message_template("banned_user_message", lang))
            return
        
        if user_id != ADMIN_ID:
            # P7-72: the bypass previously matched ONLY the primary ADMIN_ID -
            # additional admins (added from the panel) were pushed through the
            # membership gate. is_admin() covers the primary + panel admins.
            from bot.handlers.admin.router import is_admin
            from bot.middleware.channel_guard import ensure_membership, send_join_prompt
            if not is_admin(user_id):
                is_member = await ensure_membership(client, user_id)
                if not is_member:
                    await send_join_prompt(client, user_id, message=message)
                    return
        
        countries = get_open_countries()

        if not countries:
            await message.reply_text(user_t("cap_no_countries", user_id))
            return

        total = len(countries)
        response = build_capacity_response(user_id, countries, total)

        await message.reply_text(response)
