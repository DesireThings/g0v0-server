"""Seed matchmaking pools so the spectator queue can actually find a lobby.

Without rows in `matchmaking_pools` the spectator's
`MatchmakingQueueBackgroundService` returns an empty pool list on startup
and `MatchmakingJoinLobby` SignalR calls always 400. Without rows in
`matchmaking_pool_beatmaps` the queue ships an empty playlist and the
match aborts on first tick.

This script is idempotent — re-runs are safe.

Per default it creates 8 pools (one quick-play + one ranked-play for each
of the four base rulesets) and fills each with up to 60 ranked beatmaps
of the matching mode in the 60-300 second length window. All pools start
inactive (`active=0`) so the operator can flip the ones they want live
manually:

    UPDATE matchmaking_pools SET active = 1 WHERE id IN (1, 2);

Pass `--activate-quickplay-osu` to also flip the osu! quick-play pool on
in the same run (smoke-testing convenience).

Run inside the g0v0 container:

    docker exec osu_api_server sh -c "cd /app && /app/.venv/bin/python /app/tools/seed_matchmaking_pools.py"

The script uses a synchronous pymysql connection rather than the app's
async engine: the async pool's `pool_pre_ping=True` requires a greenlet
bridge that isn't always set up cleanly outside FastAPI's worker context,
and one-shot scripts don't need pool semantics anyway.
"""

from __future__ import annotations

import argparse
import os
import sys
from urllib.parse import urlparse

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pymysql
from pymysql.cursors import DictCursor

from app.config import settings


# Per ruleset: (ruleset_id, gamemode_enum_value, display_name).
_RULESETS = [
    (0, "osu", "osu! standard"),
    (1, "taiko", "taiko"),
    (2, "fruits", "catch"),
    (3, "mania", "mania"),
]

# Quick-play: 8-player free-for-all, wider initial radius for fast matchmaking.
# Difficulty / length window deliberately sane — quick play is the entry point
# for casual queueing so we want intermediate maps that the median ranked
# player can actually finish.
_DEFAULT_QUICKPLAY = {
    "lobby_size": 8,
    "rating_search_radius": 200,
    "rating_search_radius_max": 9999,
    "rating_search_radius_exp": 15,
    "min_sr": 3.0,
    "max_sr": 5.5,
    "min_length": 90,
    "max_length": 240,
}

# Ranked play: 1v1, tighter initial radius + slower expand horizon. Slightly
# higher SR floor / ceiling because the players who opt into ranked tend to
# bring more skill than the median quick-play queue.
_DEFAULT_RANKED = {
    "lobby_size": 2,
    "rating_search_radius": 100,
    "rating_search_radius_max": 9999,
    "rating_search_radius_exp": 30,
    "min_sr": 4.0,
    "max_sr": 6.5,
    "min_length": 90,
    "max_length": 240,
}

# Per-pool beatmap cap. Spectator's MATCHMAKING_POOL_SIZE default is 50;
# 60 gives us a safety margin if some get banned mid-rotation.
_BEATMAP_POOL_CAP = 60


def _connect():
    """Open a sync pymysql connection from the same DATABASE_URL the app uses.

    DATABASE_URL is `mysql+aiomysql://user:pass@host:port/db`. We strip the
    `+aiomysql` part to get back a plain `mysql://` URL pymysql understands.
    """
    url = settings.database_url
    # Pydantic Url object → str
    if hasattr(url, "unicode_string"):
        url = getattr(url, "unicode_string")()
    url = str(url).replace("mysql+aiomysql://", "mysql://").replace("mysql+pymysql://", "mysql://")
    parsed = urlparse(url)
    return pymysql.connect(
        host=parsed.hostname,
        port=parsed.port or 3306,
        user=parsed.username,
        password=parsed.password or "",
        database=parsed.path.lstrip("/"),
        cursorclass=DictCursor,
        autocommit=False,
        charset="utf8mb4",
    )


def _ensure_pool(cur, *, ruleset_id, name, pool_type, defaults):
    """Insert (or refresh tuning knobs of) a pool. Returns the row id.

    Idempotent: if the pool already exists the active flag is left alone
    so the operator's manual flips persist across re-runs.
    """
    cur.execute(
        """
        SELECT id, active FROM matchmaking_pools
        WHERE ruleset_id = %s AND name = %s AND type = %s
        """,
        (ruleset_id, name, pool_type),
    )
    row = cur.fetchone()
    if row is not None:
        cur.execute(
            """
            UPDATE matchmaking_pools
            SET lobby_size = %s,
                rating_search_radius = %s,
                rating_search_radius_max = %s,
                rating_search_radius_exp = %s
            WHERE id = %s
            """,
            (
                defaults["lobby_size"],
                defaults["rating_search_radius"],
                defaults["rating_search_radius_max"],
                defaults["rating_search_radius_exp"],
                row["id"],
            ),
        )
        return row["id"], False  # not newly created

    cur.execute(
        """
        INSERT INTO matchmaking_pools
            (ruleset_id, name, type, active,
             lobby_size, rating_search_radius, rating_search_radius_max, rating_search_radius_exp)
        VALUES
            (%s, %s, %s, 0, %s, %s, %s, %s)
        """,
        (
            ruleset_id,
            name,
            pool_type,
            defaults["lobby_size"],
            defaults["rating_search_radius"],
            defaults["rating_search_radius_max"],
            defaults["rating_search_radius_exp"],
        ),
    )
    return cur.lastrowid, True


def _fill_pool_beatmaps(cur, pool_id, gamemode, cap, *, min_sr, max_sr, min_length, max_length):
    """Top up `matchmaking_pool_beatmaps` with curated ranked maps.

    Returns (added, total_after). Existing rows are left alone — only the
    diff is inserted, so re-runs don't churn `selection_count`.

    Filters:
      - mode matches the pool's ruleset (string enum)
      - beatmap_status IN ('RANKED', 'APPROVED')   — earlier this used
        BETWEEN 1 AND 2, which MySQL silently coerced into "ENUM positions
        1 and 2" = ('GRAVEYARD', 'WIP'). Result: every pool was filled with
        graveyard junk and 40+★ joke maps. Comparing against the literal
        enum strings makes MySQL do a value-equal check and avoids that
        whole class of bugs.
      - total_length and difficulty_rating windows from the per-pool
        config (set in _DEFAULT_QUICKPLAY / _DEFAULT_RANKED). This is
        what gets the pool to "playable maps" instead of "everything
        ranked".
      - ORDER BY id ASC keeps the selection deterministic across runs
        — admins can always trust that the first re-run produces the
        same maps and won't see selection_count get nuked.
    """
    cur.execute(
        "SELECT beatmap_id FROM matchmaking_pool_beatmaps WHERE pool_id = %s",
        (pool_id,),
    )
    existing_ids = {row["beatmap_id"] for row in cur.fetchall()}

    needed = cap - len(existing_ids)
    if needed <= 0:
        return 0, len(existing_ids)

    cur.execute(
        """
        SELECT id FROM beatmaps
        WHERE mode = %s
          AND deleted_at IS NULL
          AND beatmap_status IN ('RANKED', 'APPROVED')
          AND total_length BETWEEN %s AND %s
          AND difficulty_rating BETWEEN %s AND %s
        ORDER BY id ASC
        """,
        (gamemode, min_length, max_length, min_sr, max_sr),
    )
    candidates = [row["id"] for row in cur.fetchall()]

    added = 0
    for bid in candidates:
        if bid in existing_ids:
            continue
        cur.execute(
            """
            INSERT INTO matchmaking_pool_beatmaps
                (pool_id, beatmap_id, mods, rating, rating_sig, selection_count)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (pool_id, bid, "[]", 1500, 150.0, 0),
        )
        added += 1
        if added >= needed:
            break

    return added, len(existing_ids) + added


def main(activate_quickplay_osu: bool, *, wipe_first: bool = False) -> None:
    conn = _connect()
    try:
        with conn.cursor() as cur:
            if wipe_first:
                # Operator opt-in. Use this when an earlier seed run polluted
                # pools with garbage maps (e.g. the historical bug where the
                # status filter matched GRAVEYARD/WIP). The pools themselves
                # stay; only their beatmap rotations get cleared so the
                # selection_count history isn't lost on un-affected pools.
                cur.execute("DELETE FROM matchmaking_pool_beatmaps")
                print(f"[!] wiped {cur.rowcount} pool_beatmap rows")

            for ruleset_id, mode, ruleset_name in _RULESETS:
                # quick-play pool
                qp_id, qp_new = _ensure_pool(
                    cur,
                    ruleset_id=ruleset_id,
                    name=f"{ruleset_name} (quick play)",
                    pool_type="quick_play",
                    defaults=_DEFAULT_QUICKPLAY,
                )
                qp_added, qp_total = _fill_pool_beatmaps(
                    cur,
                    qp_id,
                    mode,
                    _BEATMAP_POOL_CAP,
                    min_sr=_DEFAULT_QUICKPLAY["min_sr"],
                    max_sr=_DEFAULT_QUICKPLAY["max_sr"],
                    min_length=_DEFAULT_QUICKPLAY["min_length"],
                    max_length=_DEFAULT_QUICKPLAY["max_length"],
                )
                marker_qp = "[+]" if qp_new else "[=]"
                print(
                    f"{marker_qp} pool {qp_id} ruleset={ruleset_id} type=quick_play "
                    f"name={ruleset_name!r} beatmaps=+{qp_added}/{qp_total}"
                )

                # ranked-play pool
                rp_id, rp_new = _ensure_pool(
                    cur,
                    ruleset_id=ruleset_id,
                    name=f"{ruleset_name} (ranked play)",
                    pool_type="ranked_play",
                    defaults=_DEFAULT_RANKED,
                )
                rp_added, rp_total = _fill_pool_beatmaps(
                    cur,
                    rp_id,
                    mode,
                    _BEATMAP_POOL_CAP,
                    min_sr=_DEFAULT_RANKED["min_sr"],
                    max_sr=_DEFAULT_RANKED["max_sr"],
                    min_length=_DEFAULT_RANKED["min_length"],
                    max_length=_DEFAULT_RANKED["max_length"],
                )
                marker_rp = "[+]" if rp_new else "[=]"
                print(
                    f"{marker_rp} pool {rp_id} ruleset={ruleset_id} type=ranked_play "
                    f"name={ruleset_name!r} beatmaps=+{rp_added}/{rp_total}"
                )

            if activate_quickplay_osu:
                cur.execute(
                    """
                    UPDATE matchmaking_pools
                    SET active = 1
                    WHERE ruleset_id = 0 AND type = 'quick_play'
                    """
                )
                print(f"[+] activated osu! quick-play pool ({cur.rowcount} row affected)")

        conn.commit()
        print("seed complete")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--activate-quickplay-osu",
        action="store_true",
        help="Flip the osu! quick-play pool to active=1 (smoke-testing convenience).",
    )
    ap.add_argument(
        "--wipe-pool-beatmaps",
        action="store_true",
        help=(
            "Delete every row from matchmaking_pool_beatmaps before re-seeding. "
            "Use this to recover from a botched earlier seed (e.g. the GRAVEYARD "
            "leak where the legacy filter pulled in joke maps). selection_count "
            "is reset for every map but the pools themselves stay intact."
        ),
    )
    args = ap.parse_args()
    main(args.activate_quickplay_osu, wipe_first=args.wipe_pool_beatmaps)
