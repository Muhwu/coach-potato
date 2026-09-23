from server import minion_waves as mw


def _mmss(t):
    return f"{t // 60}:{t % 60:02d}"


def test_wave_timings_follow_26_1_intervals():
    times = [_mmss(w["t"]) for w in mw.waves(31 * 60)]
    assert times[:3] == ["0:30", "1:00", "1:30"]
    for a, b, c in (("13:30", "14:00", "14:25"), ("29:25", "29:50", "30:10")):
        i = times.index(a)
        assert times[i + 1:i + 3] == [b, c]


def test_cannon_waves_every_third_then_second_then_every_wave():
    cannons = [_mmss(w["t"]) for w in mw.waves(26 * 60) if w["siege"]]
    assert cannons[:2] == ["1:30", "3:00"]
    for a, b, c in (("12:00", "13:30", "14:25"), ("24:25", "25:15", "25:40")):
        i = cannons.index(a)
        assert cannons[i + 1:i + 3] == [b, c]


def test_wave_composition_and_gold_match_the_wiki_table():
    by_t = {w["t"]: w for w in mw.waves(31 * 60)}
    assert by_t[30]["gold"] == 102
    assert (by_t[865]["melee"], by_t[865]["caster"], by_t[865]["siege"]) == (2, 3, 1)
    assert by_t[1515]["gold"] == 148  # wiki: 25:00 wave
    assert (by_t[1810]["caster"], by_t[1810]["gold"]) == (2, 137)  # wiki: 30:00 wave


def test_max_farm_counts_waves_once_they_reach_lane():
    counts, golds = mw.max_farm([0, 1, 2])
    # 0:30 wave in lane at 1:00; by 2:00 also the 1:00 wave and the 1:30 cannon wave
    assert counts == [0, 6, 19]
    assert golds[1] == 102 and golds[2] == 102 * 3 + 50


def test_supports_version():
    # match-v5 reports patch 26.x as 16.x (internal numbering)
    assert mw.supports_version("16.1.123.4")
    assert mw.supports_version("16.19.700.1")
    assert mw.supports_version("17.0.1")
    assert not mw.supports_version("15.24.1")  # a 2025 patch
    assert not mw.supports_version("14.1.1")
    assert not mw.supports_version(None) and not mw.supports_version("junk")


def test_estimate_minion_gold_prices_kills_at_that_minutes_bounty():
    counts, golds = mw.max_farm([0, 1, 2])
    assert mw.estimate_minion_gold([0, 1, 2], [0, 6, 19], counts, golds) == [0, 102, golds[2]]
    assert mw.estimate_minion_gold([0, 1], [0, None], counts[:2], golds[:2]) == [0, None]
