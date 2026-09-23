"""Theoretical "perfect farm" benchmark: every lane minion that could have
been last-hit by a given minute, and the gold they were worth.

Rules are Summoner's Rift as of patch 26.1 (League Wiki, "Minion" /
"Siege minion" pages, and the 26.1 patch notes):

- The first wave spawns at 0:30. Waves come every 30 s until 14:00, every
  25 s from then (13:30 / 14:00 / 14:25), every 20 s from 30:00
  (29:25 / 29:50 / 30:10).
- A wave is 3 melee + 3 caster. A siege (cannon) minion joins the 3rd wave
  (1:30) and then every 3rd wave; every 2nd wave from 14:00
  (12:00 / 13:30 / 14:25 / 15:15); every wave from 25:00
  (24:25 / 25:15 / 25:40).
- From 14:00 a wave carrying a cannon has one melee fewer; from 30:00 every
  wave has one caster fewer.
- Bounties: melee 20, caster 14, siege 50 + 1 per minion upgrade (upgrades
  every 90 s from 0:30). Checked against the wiki's per-wave gold table
  (148 at 25:00, 137 at 30:00).

Deliberately left out, so this is a ceiling for ONE lane under normal
conditions: super minions (they need an inhibitor down), jungle camps, and
the travel/kill time differing per lane (one flat LANE_TRAVEL_S). Games
before 26.1 used different rules (1:05 first wave, 30 s all game), so they
get no benchmark rather than a wrong one.
"""

FIRST_WAVE_S = 30
LANE_TRAVEL_S = 30  # spawn → last-hittable in lane, approximate
MELEE_GOLD, CASTER_GOLD, SIEGE_BASE_GOLD = 20, 14, 50
UPGRADE_EVERY_S = 90
MIN_PATCH = (26, 1)

# roles whose farm is lane minions; a jungler's CS is camps, not waves
LANE_ROLES = {"TOP", "MIDDLE", "BOTTOM", "UTILITY"}


def supports_version(game_version):
    """True for a match-v5 gameVersion ("26.14.612.1234") on or after 26.1."""
    try:
        major, minor = (int(p) for p in (game_version or "").split(".")[:2])
    except ValueError:
        return False
    return (major, minor) >= MIN_PATCH


def _next_wave(t):
    if t < 14 * 60:
        return t + 30
    if t + 25 <= 30 * 60:
        return t + 25
    return t + 20


def _cannon_gap(t):
    if t < 14 * 60:
        return 3
    if t < 25 * 60:
        return 2
    return 1


def siege_gold(t):
    return SIEGE_BASE_GOLD + max(0, (t - FIRST_WAVE_S) // UPGRADE_EVERY_S)


def waves(until_s):
    """[{t, melee, caster, siege, gold}] for every wave spawned by until_s."""
    out = []
    t, index, last_cannon = FIRST_WAVE_S, 1, None
    while t <= until_s:
        if last_cannon is None:
            cannon = index == 3
        else:
            cannon = index - last_cannon >= _cannon_gap(t)
        if cannon:
            last_cannon = index
        melee = 3 - (1 if cannon and t >= 14 * 60 else 0)
        caster = 3 - (1 if t >= 30 * 60 else 0)
        siege = 1 if cannon else 0
        gold = melee * MELEE_GOLD + caster * CASTER_GOLD + siege * siege_gold(t)
        out.append({"t": t, "melee": melee, "caster": caster,
                    "siege": siege, "gold": gold})
        t = _next_wave(t)
        index += 1
    return out


def max_farm(minutes):
    """Cumulative (minions, gold) a perfect last-hitter could have by each
    minute mark: every minion whose wave is last-hittable (spawn +
    LANE_TRAVEL_S) at or before minute*60."""
    if not minutes:
        return [], []
    arrivals = [(w["t"] + LANE_TRAVEL_S, w["melee"] + w["caster"] + w["siege"], w["gold"])
                for w in waves(max(minutes) * 60)]
    counts, golds = [], []
    for m in minutes:
        cutoff = m * 60
        counts.append(sum(n for at, n, _ in arrivals if at <= cutoff))
        golds.append(sum(g for at, _, g in arrivals if at <= cutoff))
    return counts, golds


def estimate_minion_gold(minutes, killed, max_counts, max_golds):
    """Riot reports minions killed per frame, not the gold they paid. Price
    each minute's newly-killed minions at the average bounty of the minions
    that reached lane in that same minute (falling back to the running
    average when none did) and accumulate. None entries in `killed` stay
    None."""
    out, total, prev_killed = [], 0.0, 0
    prev_count, prev_gold = 0, 0
    avg = (3 * MELEE_GOLD + 3 * CASTER_GOLD) / 6
    for i, _ in enumerate(minutes):
        arrived = max_counts[i] - prev_count
        if arrived > 0:
            avg = (max_golds[i] - prev_gold) / arrived
        prev_count, prev_gold = max_counts[i], max_golds[i]
        k = killed[i]
        if k is None:
            out.append(None)
            continue
        total += max(0, k - prev_killed) * avg
        prev_killed = k
        out.append(round(total))
    return out
