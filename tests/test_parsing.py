import json
from pathlib import Path

from server.parsing import parse_match

FIXTURE = Path(__file__).parent / "fixtures" / "match_sample.json"


def load_fixture():
    return json.loads(FIXTURE.read_text())


def test_parse_match_maps_match_fields():
    match_row, _ = parse_match(load_fixture())
    assert match_row == {
        "match_id": "EUW1_7000000001",
        "queue_id": 420,
        "game_creation_ms": 1719000000000,
        "game_duration_s": 1856,
        "game_version": "14.13.596.7996",
    }


def test_parse_match_returns_ten_participants():
    _, parts = parse_match(load_fixture())
    assert len(parts) == 10


def test_participant_field_mapping_and_cs_sum():
    _, parts = parse_match(load_fixture())
    p0 = next(p for p in parts if p["puuid"] == "puuid-0")
    assert p0["riot_id_name"] == "Player0"
    assert p0["champion_name"] == "Garen"
    assert p0["team_id"] == 100
    assert p0["team_position"] == "TOP"
    assert p0["win"] == 1
    assert (p0["kills"], p0["deaths"], p0["assists"]) == (0, 2, 10)
    assert p0["cs"] == 150 + 20  # lane + jungle minions
    assert p0["gold_earned"] == 11000
    assert p0["damage_to_champions"] == 15000


def test_participant_loadout_spells_and_items():
    _, parts = parse_match(load_fixture())
    p0 = next(p for p in parts if p["puuid"] == "puuid-0")
    # fixture omits summoner ids (default 0) and has only item0 set
    assert p0["summoner1_id"] == 0 and p0["summoner2_id"] == 0
    assert json.loads(p0["items"]) == [3078, 0, 0, 0, 0, 0, 0]  # 7 slots, absent -> 0
    p9 = next(p for p in parts if p["puuid"] == "puuid-9")
    assert p9["win"] == 0
    assert p9["team_id"] == 200


def test_missing_optional_fields_default_gracefully():
    data = load_fixture()
    p = data["info"]["participants"][0]
    del p["riotIdGameName"]
    del p["neutralMinionsKilled"]
    _, parts = parse_match(data)
    p0 = next(q for q in parts if q["puuid"] == "puuid-0")
    assert p0["riot_id_name"] == ""
    assert p0["cs"] == 150
