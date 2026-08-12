"""Incremental, idempotent match-history crawler + rank enrichment."""
import json
import time

from . import db, rune_data
from .metrics import (parse_build_order, parse_death_events, parse_jungle_starts,
                      parse_metrics,
                      parse_skill_order, parse_starting_items, parse_timeline_deltas)
from .parsing import parse_match

_NO_TIMELINE = object()  # _store_metrics sentinel: no timeline fetch was attempted

RANK_TTL_MS = 7 * 86_400_000  # re-fetch a player's rank after 7 days
PAGE_SIZE = 100
OVERLAP_S = 3600  # re-scan 1 h before the watermark to be safe


def _default_now_ms():
    return int(time.time() * 1000)


class Crawler:
    def __init__(self, client, conn, status_cb=None, now_ms=_default_now_ms):
        self.client = client
        self.conn = conn
        self.status_cb = status_cb or (lambda msg: None)
        self.now_ms = now_ms

    def crawl_player(self, game_name, tag_line, queues=(420, 440), limit=None,
                     is_tracked=True, since_s=None, fetch_timeline=True):
        """Fetch and store all new matches for one account.

        limit caps the number of *new* match-detail fetches (across queues),
        for small test batches. An interrupted/limited crawl leaves the
        watermark incomplete so the next run re-pages full history (details
        already stored are skipped, so this is cheap).

        is_tracked=False crawls a comparison ("research") player: their match
        detail/metrics/runes are stored the same way (so the Matchup guide can
        compare against them) but their `players` row stays untracked, keeping
        them out of your own tracked stats. upsert never demotes an existing
        tracked player (is_tracked is MAX-merged).

        since_s (epoch seconds) bounds the match-id lookup to games on/after
        that time — used to keep comparison-player fetches to a small recent
        window (e.g. the last 7 days) so the API isn't asked for a stranger's
        entire history.
        """
        account = self.client.get_account(game_name, tag_line)
        puuid = account["puuid"]
        db.upsert_player(self.conn, puuid, account.get("gameName", game_name),
                         account.get("tagLine", tag_line), is_tracked=is_tracked)
        new_matches = 0
        for queue in queues:
            newest_ms, complete = db.get_crawl_watermark(self.conn, puuid, queue)
            start_time = since_s  # comparison lookback window (epoch s), if given
            if start_time is None and complete and newest_ms:
                start_time = max(0, newest_ms // 1000 - OVERLAP_S)
            start = 0
            reached_limit = False
            while True:
                ids = self.client.get_match_ids(
                    puuid, queue=queue, start=start, count=PAGE_SIZE, start_time=start_time
                )
                for match_id in ids:
                    # skip only if THIS player is already stored for the match —
                    # not merely if the match row exists. A match first crawled
                    # via another player (or whose participant rows were purged
                    # and the account re-added) still needs this player's row.
                    if db.has_participant(self.conn, match_id, puuid):
                        continue
                    if limit is not None and new_matches >= limit:
                        reached_limit = True
                        break
                    match_json = self.client.get_match(match_id)
                    match_row, participant_rows = parse_match(match_json)
                    # insert_match OR-IGNOREs, so it backfills this player's
                    # participant row into an already-stored match too
                    db.insert_match(self.conn, match_row, participant_rows)
                    if fetch_timeline:
                        timeline = self._safe_timeline(match_id)
                        self._store_metrics(match_json, timeline)
                        # same timeline, no extra API call
                        self._store_jungle_starts(match_id, timeline)
                    else:  # skip the timeline fetch (halves API calls) — no lane Δ
                        self._store_metrics(match_json)
                    self._store_runes(match_json)
                    new_matches += 1
                    self.status_cb(
                        f"{game_name}#{tag_line} queue {queue}: stored {match_id} "
                        f"({new_matches} new)"
                    )
                if reached_limit or len(ids) < PAGE_SIZE:
                    break
                start += PAGE_SIZE
            newest_stored = self.conn.execute(
                """SELECT MAX(m.game_creation_ms) AS newest FROM matches m
                   JOIN participants p ON p.match_id = m.match_id
                   WHERE p.puuid=? AND m.queue_id=?""",
                (puuid, queue),
            ).fetchone()["newest"]
            db.set_crawl_watermark(self.conn, puuid, queue,
                                   newest_ms=newest_stored, complete=not reached_limit)
        return {"puuid": puuid, "new_matches": new_matches}

    def _tracked_puuids(self):
        return {r["puuid"] for r in
                self.conn.execute("SELECT puuid FROM players WHERE is_tracked=1")}

    def _stored_puuids(self):
        """Tracked players plus comparison ('research') players — everyone
        whose per-match metrics and runes we store, so the Matchup guide can
        compare you against them. Comparison players stay out of tracked-only
        stats; this set is only about what per-match detail we keep."""
        return {r["puuid"] for r in self.conn.execute(
            "SELECT puuid FROM players WHERE is_tracked=1 "
            "UNION SELECT puuid FROM comparison_players")}

    def _lane_opponent(self, match_json, puuid):
        """Enemy in the same teamPosition (the direct lane opponent), or None."""
        participants = match_json["info"]["participants"]
        me = next((p for p in participants if p["puuid"] == puuid), None)
        if not me or not me.get("teamPosition"):
            return None
        enemy = next((q for q in participants
                      if q["teamId"] != me["teamId"]
                      and q.get("teamPosition") == me["teamPosition"]), None)
        return enemy["puuid"] if enemy else None

    def _safe_timeline(self, match_id):
        """Fetch the match timeline, tolerating failure — lane deltas are a
        bonus, not worth aborting a crawl over (older matches can 404)."""
        try:
            return self.client.get_match_timeline(match_id)
        except Exception:  # noqa: BLE001 — never let a timeline break the crawl
            return None

    def _store_jungle_starts(self, match_id, timeline):
        """Record which map half each team's jungler started in (strong/weak
        side). Match-level, so it's stored once per match rather than per
        tracked participant, and it always writes — a match with no
        identifiable jungler records '' so the backfill doesn't retry it."""
        junglers = db.jungler_puuids(self.conn, match_id)
        db.set_match_jungle_starts(
            self.conn, match_id, parse_jungle_starts(timeline, junglers))

    def _store_metrics(self, match_json, timeline=_NO_TIMELINE):
        """Store challenge/participant metrics for tracked players. When a
        timeline was fetched (crawl path — pass it even if the fetch returned
        None), also fill the lane-delta columns, the death-location map
        events, and mark has_timeline=1/has_map_events=1 so the backfills skip
        the match. Omitting `timeline` (backfill_metrics path) leaves the
        timeline-derived columns/rows untouched."""
        stored = self._stored_puuids()
        match_id = match_json["metadata"]["matchId"]
        attempted_timeline = timeline is not _NO_TIMELINE
        for participant in match_json["info"]["participants"]:
            if participant["puuid"] not in stored:
                continue
            puuid = participant["puuid"]
            values = parse_metrics(match_json, puuid)
            if attempted_timeline:
                opp = self._lane_opponent(match_json, puuid)
                values.update(parse_timeline_deltas(timeline, puuid, opp))  # None -> all None
                values["has_timeline"] = 1
            db.insert_participant_metrics(self.conn, match_id, puuid, values)
            if attempted_timeline:  # start buy + build order + skill order, one timeline
                final_items = [participant.get(f"item{i}") for i in range(7)]
                db.update_participant_timeline_items(
                    self.conn, match_id, puuid,
                    parse_starting_items(timeline, puuid),
                    parse_build_order(timeline, puuid, final_items),
                    parse_skill_order(timeline, puuid))
                deaths = parse_death_events(timeline, puuid) or []
                events = [{"event_type": "death", **d} for d in deaths]
                db.replace_map_events(self.conn, match_id, puuid, events)

    def backfill_metrics(self, limit=None):
        """Re-fetch details for stored matches whose tracked participants
        lack a participant_metrics row. Returns matches fetched."""
        rows = self.conn.execute(
            """SELECT DISTINCT p.match_id FROM participants p
               JOIN players pl ON pl.puuid = p.puuid
                 AND (pl.is_tracked = 1 OR pl.puuid IN (SELECT puuid FROM comparison_players))
               LEFT JOIN participant_metrics pm
                 ON pm.match_id = p.match_id AND pm.puuid = p.puuid
               WHERE pm.match_id IS NULL"""
        ).fetchall()
        count = 0
        for row in rows:
            if limit is not None and count >= limit:
                break
            self._store_metrics(self.client.get_match(row["match_id"]))
            count += 1
            self.status_cb(f"metrics backfill: {count}/{len(rows)} matches")
        return count

    def _store_runes(self, match_json):
        """Stores runes for tracked participants AND their lane opponent
        (same teamPosition, other team) — the Overview/Champ-guide recent-
        games lists show both sides of the matchup, not just your own pick.
        Comparison ('research') players are stored too (same as tracked), so
        their actual rune pages are available in the guide comparison."""
        stored = self._stored_puuids()
        match_id = match_json["metadata"]["matchId"]
        participants = match_json["info"]["participants"]
        by_puuid = {p["puuid"]: p for p in participants}
        wanted = set()
        for p in participants:
            if p["puuid"] not in stored:
                continue
            wanted.add(p["puuid"])
            pos = p.get("teamPosition")
            if not pos:
                continue
            enemy = next((q for q in participants
                         if q["teamId"] != p["teamId"] and q.get("teamPosition") == pos), None)
            if enemy:
                wanted.add(enemy["puuid"])
        for puuid in wanted:
            runes = rune_data.decode_perks(by_puuid[puuid].get("perks"))
            db.insert_participant_runes(self.conn, match_id, puuid, runes)

    def backfill_runes(self, limit=None):
        """Re-fetch details for stored matches missing a participant_runes
        row for a tracked participant or their lane opponent. Returns
        matches fetched."""
        rows = self.conn.execute(
            """SELECT DISTINCT me.match_id FROM participants me
               JOIN players pl ON pl.puuid = me.puuid
                 AND (pl.is_tracked = 1 OR pl.puuid IN (SELECT puuid FROM comparison_players))
               LEFT JOIN participant_runes pr_me
                 ON pr_me.match_id = me.match_id AND pr_me.puuid = me.puuid
               LEFT JOIN participants opp
                 ON opp.match_id = me.match_id AND opp.team_id != me.team_id
                    AND opp.team_position = me.team_position AND me.team_position != ''
               LEFT JOIN participant_runes pr_opp
                 ON pr_opp.match_id = opp.match_id AND pr_opp.puuid = opp.puuid
               WHERE pr_me.match_id IS NULL
                  OR (opp.puuid IS NOT NULL AND pr_opp.match_id IS NULL)"""
        ).fetchall()
        count = 0
        for row in rows:
            if limit is not None and count >= limit:
                break
            self._store_runes(self.client.get_match(row["match_id"]))
            count += 1
            self.status_cb(f"runes backfill: {count}/{len(rows)} matches")
        return count

    def backfill_items(self, limit=None):
        """Re-fetch details for stored TRACKED matches whose participant rows
        lack loadout data (summoner spells + items) — rows stored before loadout
        tracking don't have them, and INSERT OR IGNORE never overwrites on a
        re-crawl. Scoped to tracked accounts on purpose: a single RiotClient is
        region-bound, while comparison ('research') players can be on other
        regions — those are re-fetched region-correctly by their own on-demand
        comparison crawl (which now stores loadout too). A per-match fetch
        failure (an old match 404, a stray cross-region id) is skipped, not
        fatal. Returns matches successfully re-fetched."""
        rows = self.conn.execute(
            """SELECT DISTINCT me.match_id FROM participants me
               JOIN players pl ON pl.puuid = me.puuid AND pl.is_tracked = 1
               WHERE me.items IS NULL"""
        ).fetchall()
        count = 0
        for i, row in enumerate(rows):
            if limit is not None and count >= limit:
                break
            try:
                self._store_items(self.client.get_match(row["match_id"]))
            except Exception:  # noqa: BLE001 — one bad match must not abort the run
                continue
            count += 1
            self.status_cb(f"items backfill: {count}/{len(rows)} matches")
        return count

    def backfill_items_for_player(self, puuid, limit=None):
        """Fill loadout (spells + items) for one player's stored matches that
        lack it, using THIS crawler's client — so the caller controls the
        region (comparison players can be on another region than you; build the
        client with their platform). Tolerant of per-match fetch failures.
        Returns matches successfully re-fetched."""
        # Newest first: the guide comparison shows a player's most recent games,
        # so fill those before older history — the visible rows populate first.
        rows = self.conn.execute(
            """SELECT pa.match_id FROM participants pa
               JOIN matches m ON m.match_id = pa.match_id
               WHERE pa.puuid=? AND pa.items IS NULL
               ORDER BY m.game_creation_ms DESC""",
            (puuid,)).fetchall()
        count = 0
        for row in rows:
            if limit is not None and count >= limit:
                break
            try:
                self._store_items(self.client.get_match(row["match_id"]))
            except Exception:  # noqa: BLE001 — one bad match must not abort the run
                continue
            count += 1
            self.status_cb(f"items backfill (player): {count}/{len(rows)} matches")
        return count

    def backfill_timeline_items_for_player(self, puuid, limit=None):
        """Fill timeline-derived item info (game-start buy + build order) for one
        player's stored matches missing it, by fetching each match's TIMELINE
        with this crawler's (region-specific) client. Newest first (the
        comparison shows recent games). Build order is derived against that
        player's stored final inventory. A failed/absent timeline stores empty
        lists so it isn't retried forever. Returns matches processed."""
        rows = self.conn.execute(
            """SELECT pa.match_id, pa.items FROM participants pa
               JOIN matches m ON m.match_id = pa.match_id
               WHERE pa.puuid=? AND pa.skill_order IS NULL
               ORDER BY m.game_creation_ms DESC""",
            (puuid,)).fetchall()
        count = 0
        for row in rows:
            if limit is not None and count >= limit:
                break
            timeline = self._safe_timeline(row["match_id"])
            final_items = json.loads(row["items"]) if row["items"] else []
            db.update_participant_timeline_items(
                self.conn, row["match_id"], puuid,
                parse_starting_items(timeline, puuid) or [],
                parse_build_order(timeline, puuid, final_items) or [],
                parse_skill_order(timeline, puuid) or [])
            count += 1
            self.status_cb(f"timeline-items backfill: {count}/{len(rows)} matches")
        return count

    def _store_items(self, match_json):
        """Fill loadout (summoner spells + items) for stored tracked/comparison
        participant rows from a fetched match detail. Only updates rows that
        exist (a stranger's row we don't keep is skipped)."""
        stored = self._stored_puuids()
        match_id = match_json["metadata"]["matchId"]
        for p in match_json["info"]["participants"]:
            if p["puuid"] not in stored:
                continue
            items = [p.get(f"item{i}", 0) for i in range(7)]
            db.update_participant_loadout(
                self.conn, match_id, p["puuid"],
                p.get("summoner1Id", 0), p.get("summoner2Id", 0), items)

    def backfill_lane_deltas(self, limit=None, block_games_only=False):
        """Fetch the match timeline for tracked-participant metrics rows that
        don't have lane deltas yet (has_timeline=0) and fill in the ΔCS/level/
        gold-vs-opponent columns. The lane opponent comes from the stored
        participants (same team_position, other team), so this needs only the
        timeline — not the match detail. A missing/failed timeline still marks
        the row done (blank deltas) so it isn't retried forever.
        block_games_only restricts to games sitting in a block (used by the
        web app to deepen block insights proactively). Returns matches fetched."""
        block_filter = ("AND EXISTS (SELECT 1 FROM block_games bg "
                        "WHERE bg.match_id = pm.match_id AND bg.puuid = pm.puuid)"
                        if block_games_only else "")
        rows = self.conn.execute(
            f"""SELECT me.match_id, me.puuid, opp.puuid AS opp_puuid
               FROM participant_metrics pm
               JOIN participants me ON me.match_id = pm.match_id AND me.puuid = pm.puuid
               JOIN players pl ON pl.puuid = me.puuid
                 AND (pl.is_tracked = 1 OR pl.puuid IN (SELECT puuid FROM comparison_players))
               LEFT JOIN participants opp
                 ON opp.match_id = me.match_id AND opp.team_id != me.team_id
                    AND opp.team_position = me.team_position AND me.team_position != ''
               WHERE pm.has_timeline = 0 {block_filter}"""
        ).fetchall()
        count = 0
        for row in rows:
            if limit is not None and count >= limit:
                break
            timeline = self._safe_timeline(row["match_id"])
            deltas = parse_timeline_deltas(timeline, row["puuid"], row["opp_puuid"])
            db.update_participant_timeline(self.conn, row["match_id"], row["puuid"], deltas)
            # the jungle start halves come off the same timeline — filling them
            # here means most installs never need the dedicated backfill below
            self._store_jungle_starts(row["match_id"], timeline)
            count += 1
            self.status_cb(f"lane-delta backfill: {count}/{len(rows)} matches")
        return count

    def backfill_jungle_starts(self, limit=None, block_games_only=False):
        """Fetch timelines for matches whose jungle start halves were never
        looked at (jungle_start_100 IS NULL) and fill them in — i.e. matches
        stored before strong/weak-side detection existed, whose timeline was
        already processed for lane deltas so backfill_lane_deltas skips them.
        Restricted to matches a tracked/comparison player actually played.
        block_games_only narrows to games sitting in a block, matching the
        lane-delta backfill. Returns matches fetched."""
        block_filter = ("AND EXISTS (SELECT 1 FROM block_games bg "
                        "WHERE bg.match_id = m.match_id)"
                        if block_games_only else "")
        rows = self.conn.execute(
            f"""SELECT DISTINCT m.match_id FROM matches m
                JOIN participants p ON p.match_id = m.match_id
                JOIN players pl ON pl.puuid = p.puuid
                  AND (pl.is_tracked = 1 OR pl.puuid IN (SELECT puuid FROM comparison_players))
                WHERE m.jungle_start_100 IS NULL {block_filter}
                ORDER BY m.match_id"""
        ).fetchall()
        count = 0
        for row in rows:
            if limit is not None and count >= limit:
                break
            self._store_jungle_starts(row["match_id"], self._safe_timeline(row["match_id"]))
            count += 1
            self.status_cb(f"jungle-side backfill: {count}/{len(rows)} matches")
        return count

    def backfill_map_events(self, limit=None, block_games_only=False):
        """Fetch the match timeline for tracked/comparison-participant metrics
        rows that don't have death-location map events yet (has_map_events=0)
        and store them. Mirrors backfill_lane_deltas exactly (same idle-column
        marker idiom, same tolerant-of-missing-timeline behavior — a missing/
        failed timeline still marks the row done with an empty event list so
        it isn't retried forever). block_games_only restricts to games sitting
        in a block. Returns matches fetched."""
        block_filter = ("AND EXISTS (SELECT 1 FROM block_games bg "
                        "WHERE bg.match_id = pm.match_id AND bg.puuid = pm.puuid)"
                        if block_games_only else "")
        rows = self.conn.execute(
            f"""SELECT me.match_id, me.puuid
               FROM participant_metrics pm
               JOIN participants me ON me.match_id = pm.match_id AND me.puuid = pm.puuid
               JOIN players pl ON pl.puuid = me.puuid
                 AND (pl.is_tracked = 1 OR pl.puuid IN (SELECT puuid FROM comparison_players))
               WHERE pm.has_map_events = 0 {block_filter}"""
        ).fetchall()
        count = 0
        for row in rows:
            if limit is not None and count >= limit:
                break
            timeline = self._safe_timeline(row["match_id"])
            deaths = parse_death_events(timeline, row["puuid"]) or []
            events = [{"event_type": "death", **d} for d in deaths]
            db.replace_map_events(self.conn, row["match_id"], row["puuid"], events)
            count += 1
            self.status_cb(f"map-event backfill: {count}/{len(rows)} matches")
        return count

    def _stale_before(self):
        return self.now_ms() - RANK_TTL_MS

    def enrich_ranks(self, max_players=None):
        """Fetch current solo rank for lane opponents of tracked players.

        Only opponents who shared a lane (same team_position, other team)
        with a tracked player are looked up; results are cached for
        RANK_TTL_MS.
        """
        rows = self.conn.execute(
            """SELECT DISTINCT opp.puuid AS puuid
               FROM participants me
               JOIN participants opp ON opp.match_id = me.match_id
                AND opp.team_id != me.team_id
                AND opp.team_position = me.team_position
               JOIN players pl ON pl.puuid = me.puuid
                 AND (pl.is_tracked = 1 OR pl.puuid IN (SELECT puuid FROM comparison_players))
               LEFT JOIN player_ranks pr ON pr.puuid = opp.puuid
               WHERE me.team_position != ''
                 AND (pr.puuid IS NULL OR pr.fetched_at_ms < ?)""",
            (self._stale_before(),),
        ).fetchall()
        count = 0
        for row in rows:
            if max_players is not None and count >= max_players:
                break
            tier, division, lp = self._fetch_solo_rank(row["puuid"])
            db.set_player_rank(self.conn, row["puuid"], tier, division, lp,
                               fetched_at_ms=self.now_ms())
            count += 1
            self.status_cb(f"rank enrichment: {count}/{len(rows)} players")
        return count

    def refresh_tracked_ranks(self):
        rows = self.conn.execute("SELECT puuid FROM players WHERE is_tracked=1").fetchall()
        for row in rows:
            tier, division, lp = self._fetch_solo_rank(row["puuid"])
            now_ms = self.now_ms()
            with self.conn:
                self.conn.execute(
                    """UPDATE players SET solo_tier=?, solo_division=?, solo_lp=?,
                       rank_fetched_at_ms=? WHERE puuid=?""",
                    (tier, division, lp, now_ms, row["puuid"]),
                )
            if tier is not None:  # unranked snapshots are noise for the chart
                db.record_rank_history(self.conn, row["puuid"], tier, division, lp, now_ms)

    def _fetch_solo_rank(self, puuid):
        entries = self.client.get_league_entries(puuid)
        solo = next((e for e in entries if e.get("queueType") == "RANKED_SOLO_5x5"), None)
        if solo is None:
            return (None, None, None)
        return (solo.get("tier"), solo.get("rank"), solo.get("leaguePoints"))
