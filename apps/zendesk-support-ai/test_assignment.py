"""担当割り当ての単体テスト(SPEC_ASSIGNMENT.md §11)。LLM/Zendesk 不要。"""

import poster
import common

AGENTS = [{"id": 101, "name": "A"}, {"id": 102, "name": "B"}, {"id": 103, "name": "C"}]
ESC = {"scheduler": 201, "storage": None}  # storage は未定義(null)


def r(difficulty, category, cursor, agents=AGENTS, esc=ESC):
    return poster.resolve_assignee(difficulty, category, cursor=cursor,
                                   light_agents=agents, escalation_map=esc)


def test_roundrobin_advances():
    # low/normal は輪番が順に回り cursor が進む
    aid, nc, via = r("low", "other", 0)
    assert (aid, nc, via) == (101, 1, "roundrobin"), (aid, nc, via)
    aid, nc, via = r("normal", "network", 1)
    assert (aid, nc, via) == (102, 2, "roundrobin")
    # 一周して戻る
    aid, nc, via = r("low", "other", 3)
    assert aid == 101 and nc == 4


def test_high_escalation_no_advance():
    # high は専門担当から引かれ cursor が進まない
    aid, nc, via = r("high", "scheduler", 5)
    assert (aid, nc, via) == (201, 5, "escalation"), (aid, nc, via)


def test_high_escalation_list_deterministic():
    # escalation_map[category] がリストなら seed(=ticket_id)で決定論的に1人選び、cursor は進めない
    esc = {"software": [301, 302]}
    aid_even, nc, via = r("high", "software", 5, esc=esc)  # seed 既定 0 -> index 0
    assert (aid_even, nc, via) == (301, 5, "escalation")
    # seed を変えると選択が変わりうる(分散)
    a0 = poster.resolve_assignee("high", "software", cursor=5, light_agents=AGENTS,
                                 escalation_map=esc, seed=10)[0]
    a1 = poster.resolve_assignee("high", "software", cursor=5, light_agents=AGENTS,
                                 escalation_map=esc, seed=11)[0]
    assert a0 == 301 and a1 == 302  # 偶奇で振り分く
    # null 混じりのリストは除去される
    aid, _, via = poster.resolve_assignee("high", "software", cursor=0, light_agents=AGENTS,
                                          escalation_map={"software": [None, 305]}, seed=0)
    assert aid == 305 and via == "escalation"


def test_high_empty_list_falls_back():
    aid, nc, via = poster.resolve_assignee("high", "software", cursor=0, light_agents=AGENTS,
                                           escalation_map={"software": [None]}, seed=0)
    assert aid == 101 and nc == 1 and via == "escalation->roundrobin"


def test_high_fallback_when_null():
    # high かつ escalation_map[category] が null なら輪番フォールバック(輪番を消費)
    aid, nc, via = r("high", "storage", 0)
    assert aid == 101 and nc == 1 and via == "escalation->roundrobin"


def test_high_fallback_when_category_absent():
    # category が map に無い場合も輪番フォールバック
    aid, nc, via = r("high", "compiler", 2)
    assert aid == 103 and nc == 3 and via == "escalation->roundrobin"


def test_empty_roster_returns_none():
    aid, nc, via = r("low", "other", 0, agents=[])
    assert aid is None and nc == 0


def test_validate_rejects_bad_difficulty():
    base = {"ticket_id": 1, "severity": "not_assessed", "category": "other",
            "difficulty": "URGENT", "answer_confidence": "low",
            "requires_environment_knowledge": False, "requires_runbook": False,
            "requires_operator_check": True, "safe_to_reply_to_user": False,
            "suggested_next_action": "review before replying",
            "note_body": "x" * 50, "mask_mapping": {}}
    try:
        poster.validate(base)
        assert False, "should have raised"
    except poster.ValidationError as e:
        assert "difficulty" in str(e)
    # 正常な difficulty は通る
    base["difficulty"] = "high"
    poster.validate(base)


def test_allowlist_concept():
    # resolve は allowlist 外 ID(escalation の専門担当が light_agents に無い場合)も「選ぶ」。
    # 弾くのは呼び出し側(poster.process_one)の責務であることを明示するテスト。
    aid, nc, via = poster.resolve_assignee("high", "scheduler", cursor=0,
                                           light_agents=AGENTS, escalation_map={"scheduler": 999})
    assert aid == 999  # 選ばれはする
    assert 999 not in {a["id"] for a in AGENTS}  # が allowlist 外 → poster が failed/ に弾く


def test_escalation_map_resolves_names_and_email():
    agents = [
        {"id": 101, "name": "Alice Example", "email": "alice@example.com"},
        {"id": 102, "name": "Bob Example", "email": "bob@example.com"},
    ]
    raw = {
        "scheduler": ["Alice Example"],
        "storage": "bob@example.com",
        "other": None,
    }
    resolved, errors = common.resolve_escalation_map(raw, agents)
    assert errors == []
    assert resolved == {"scheduler": [101], "storage": 102, "other": None}


def test_escalation_map_reports_unknown_name():
    resolved, errors = common.resolve_escalation_map({"network": ["Nobody"]}, AGENTS)
    assert resolved == {"network": None}
    assert errors and "Nobody" in errors[0]


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok:", name)
    print("OK: all assignment tests passed")
