"""Incremental, idempotent match-history crawler + rank enrichment."""
import time

from . import db
from .metrics import parse_metrics
from .parsing import parse_match

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
                        self._store_metrics(match_json)
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

    def _store_metrics(self, match_json):
        tracked = self._tracked_puuids()
        match_id = match_json["metadata"]["matchId"]
        for participant in match_json["info"]["participants"]:
            if participant["puuid"] in tracked:
                values = parse_metrics(match_json, participant["puuid"])
                db.insert_participant_metrics(self.conn, match_id,
                                              participant["puuid"], values)

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
