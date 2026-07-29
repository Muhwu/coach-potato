#!/usr/bin/env python
"""Crawl Riot match history into the local sqlite db.

Examples:
    python crawl.py --limit 5          # small test batch per account
    python crawl.py                    # full incremental crawl (420+440)
    python crawl.py --queues 420       # solo queue only
    python crawl.py --accounts "Name#TAG" --skip-ranks
"""
import argparse
import sys

from server import db
from server.config import load_config
from server.crawler import Crawler
from server.riot_client import ApiKeyExpiredError, RiotClient


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--limit", type=int, default=None,
                        help="max NEW matches to fetch per account (test batches)")
    parser.add_argument("--queues", type=int, nargs="+", default=[420, 440],
                        help="queue ids to crawl (default: 420 440)")
    parser.add_argument("--accounts", nargs="+", default=None,
                        help='accounts as "Name#TAG" (default: from config)')
    parser.add_argument("--skip-ranks", action="store_true",
                        help="skip opponent rank enrichment")
    parser.add_argument("--backfill-metrics", action="store_true",
                        help="only backfill coaching metrics for stored matches, no crawl")
    parser.add_argument("--backfill-runes", action="store_true",
                        help="only backfill actual runes played for stored matches, no crawl")
    parser.add_argument("--backfill-lane-deltas", action="store_true",
                        help="only backfill lane ΔCS/level/gold (needs the match timeline), no crawl")
    parser.add_argument("--backfill-items", action="store_true",
                        help="only backfill summoner spells + items for stored matches, no crawl")
    args = parser.parse_args()

    config = load_config()
    if args.accounts:
        accounts = []
        for raw in args.accounts:
            name, _, tag = raw.partition("#")
            accounts.append((name, tag))
    else:
        accounts = config.accounts

    client = RiotClient(config.riot_api_key, platform=config.platform)
    conn = db.connect(config.db_path)
    crawler = Crawler(client, conn, status_cb=lambda msg: print(f"  {msg}", flush=True))

    try:
        if args.backfill_metrics:
            print("Backfilling coaching metrics for stored matches ...")
            n = crawler.backfill_metrics(limit=args.limit)
            print(f"  -> {n} matches re-fetched")
            return
        if args.backfill_runes:
            print("Backfilling actual runes played for stored matches ...")
            n = crawler.backfill_runes(limit=args.limit)
            print(f"  -> {n} matches re-fetched")
            return
        if args.backfill_lane_deltas:
            print("Backfilling lane deltas (timeline) for stored matches ...")
            n = crawler.backfill_lane_deltas(limit=args.limit)
            print(f"  -> {n} matches re-fetched")
            return
        if args.backfill_items:
            print("Backfilling summoner spells + items for stored matches ...")
            n = crawler.backfill_items(limit=args.limit)
            print(f"  -> {n} matches re-fetched")
            return
        for game_name, tag_line in accounts:
            print(f"Crawling {game_name}#{tag_line} (queues {args.queues}"
                  f"{', limit ' + str(args.limit) if args.limit else ''}) ...")
            result = crawler.crawl_player(game_name, tag_line,
                                          queues=tuple(args.queues), limit=args.limit)
            print(f"  -> {result['new_matches']} new matches")
        if not args.skip_ranks:
            print("Fetching current solo ranks of lane opponents (cached 7 days) ...")
            n = crawler.enrich_ranks()
            print(f"  -> {n} opponent ranks fetched")
            crawler.refresh_tracked_ranks()
            print("  -> tracked players' own ranks refreshed")
    except ApiKeyExpiredError as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nInterrupted — progress is saved; rerun to resume.")
        sys.exit(130)

    matches = conn.execute("SELECT COUNT(*) c FROM matches").fetchone()["c"]
    print(f"Done. Database now holds {matches} matches ({config.db_path}).")


if __name__ == "__main__":
    main()
