from scripts import eval_replay as rp


def test_replay_and_check() -> None:
    # HCX가 JSON을 두 번(남·여) 써도 qa.understand처럼 남녀 함께로 재생한다
    base = '"type": "value", "indicator": "일반혼인율", "regions": ["강원도"], "year": 2024'
    raw = f'{{{base}, "sex": "남"}}\n{{{base}, "sex": "여"}}'
    res = rp.replay("2024년 강원도 일반혼인율은 남자랑 여자 중 누가 더 높아요?", raw)
    assert res is not None and res["status"] == "ok" and len(res["result"]["split"]) == 2
    assert rp.check({"expected_status": "ok", "allowed_action": "답변"}, res) == []
    assert rp.check({"expected_status": "refuse:범위안내", "allowed_action": "범위안내"}, res)
    assert rp.replay("질문", "JSON 없음") is None
