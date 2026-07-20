"""Incremental, idempotent match-history crawler + rank enrichment."""
import time

from . import db, rune_data
from .metrics import parse_metrics, parse_timeline_deltas
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

    def crawl_player(self, game_name, tag_line, queues=(420, 440), limit=None):
        """Fetch and store all new matches for one account.

        limit caps the number of *new* match-detail fetches (across queues),
        for small test batches. An interrupted/limited crawl leaves the
        watermark incomplete so the next run re-pages full history (details
        already stored are skipped, so this is cheap).
        """
        account = self.client.get_account(game_name, tag_line)
        puuid = account["puuid"]
        db.upsert_player(self.conn, puuid, account.get("gameName", game_name),
                         account.get("tagLine", tag_line), is_tracked=True)
        new_matches = 0
        for queue in queues:
            newest_ms, complete = db.get_crawl_watermark(self.conn, puuid, queue)
            start_time = None
            if complete and newest_ms:
                start_time = max(0, newest_ms // 1000 - OVERLAP_S)
            start = 0
            reached_limit = False
            while True:
                ids = self.client.get_match_ids(
                    puuid, queue=queue, start=start, count=PAGE_SIZE, start_time=start_time
                )
                for match_id in ids:
                    if db.has_match(self.conn, match_id):
                        continue
                    if limit is not None and new_matches >= limit:
                        reached_limit = True
                        break
                    match_json = self.client.get_match(match_id)
                    match_row, participant_rows = parse_match(match_json)
                    if db.insert_match(self.conn, match_row, participant_rows):
                        timeline = self._safe_timeline(match_id)
                        self._store_metrics(match_json, timeline)
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

    def _store_metrics(self, match_json, timeline=_NO_TIMELINE):
        """Store challenge/participant metrics for tracked players. When a
        timeline was fetched (crawl path — pass it even if the fetch returned
        None), also fill the lane-delta columns and mark has_timeline=1 so the
        backfill skips the match. Omitting `timeline` (backfill_metrics path)
        leaves the timeline columns untouched."""
        tracked = self._tracked_puuids()
        match_id = match_json["metadata"]["matchId"]
        attempted_timeline = timeline is not _NO_TIMELINE
        for participant in match_json["info"]["participants"]:
            if participant["puuid"] not in tracked:
                continue
            puuid = participant["puuid"]
            values = parse_metrics(match_json, puuid)
            if attempted_timeline:
                opp = self._lane_opponent(match_json, puuid)
                values.update(parse_timeline_deltas(timeline, puuid, opp))  # None -> all None
                values["has_timeline"] = 1
            db.insert_participant_metrics(self.conn, match_id, puuid, values)

    def backfill_metrics(self, limit=None):
        """Re-fetch details for stored matches whose tracked participants
        lack a participant_metrics row. Returns matches fetched."""
        rows = self.conn.execute(
            """SELECT DISTINCT p.match_id FROM participants p
               JOIN players pl ON pl.puuid = p.puuid AND pl.is_tracked = 1
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
        games lists show both sides of the matchup, not just your own pick."""
        tracked = self._tracked_puuids()
        match_id = match_json["metadata"]["matchId"]
        participants = match_json["info"]["participants"]
        by_puuid = {p["puuid"]: p for p in participants}
        wanted = set()
        for p in participants:
            if p["puuid"] not in tracked:
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
               JOIN players pl ON pl.puuid = me.puuid AND pl.is_tracked = 1
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
               JOIN players pl ON pl.puuid = me.puuid AND pl.is_tracked = 1
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
            count += 1
            self.status_cb(f"lane-delta backfill: {count}/{len(rows)} matches")
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
               JOIN players pl ON pl.puuid = me.puuid AND pl.is_tracked = 1
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
