"""pii_mask の単体テスト(LLM 不要 — spec フェーズ3 受け入れ条件)。"""

import pii_mask


def test_basic_mask_unmask():
    text = ("田中のアドレス tanaka@example.jp に連絡。サーバ 192.168.10.5 で /home/tanaka が壊れ、"
            "アカウント u01234、電話 03-1234-5678。")
    masked, mapping = pii_mask.mask_text(text)
    # 元の識別子がマスク済みテキストに残っていない
    for raw in ["tanaka@example.jp", "192.168.10.5", "u01234", "03-1234-5678"]:
        assert raw not in masked, f"leaked: {raw}"
    # /home/ の構造は保持、ユーザー名のみ置換
    assert "/home/[USER_1]" in masked, masked
    # 完全復元
    assert pii_mask.unmask(masked, mapping) == text


def test_consistent_token_same_value():
    text = "user@x.jp が user@x.jp に2回出る"
    masked, mapping = pii_mask.mask_text(text)
    # 同一値は同一トークン -> EMAIL は1種類だけ
    emails = {tok for tok in mapping if tok.startswith("[EMAIL")}
    assert len(emails) == 1, mapping
    assert masked.count(list(emails)[0]) == 2


def test_consistent_across_fields():
    fields = ["件名 a@b.jp", "本文でも a@b.jp と c@d.jp"]
    masked, mapping = pii_mask.mask_fields(fields)
    tok_ab = mapping_inv(mapping)["a@b.jp"]
    # フィールドをまたいでも同一トークン
    assert tok_ab in masked[0] and tok_ab in masked[1]
    assert len([t for t in mapping if t.startswith("[EMAIL")]) == 2


def test_unresolved_placeholder_detection():
    _, mapping = pii_mask.mask_text("a@b.jp")
    # LLM が捏造した [EMAIL_9] は対応表に無い
    assert pii_mask.has_unresolved_placeholders("ここに [EMAIL_9] が混入", mapping)
    # 既知トークンだけなら False
    known = list(mapping.keys())[0]
    assert not pii_mask.has_unresolved_placeholders(f"{known} は既知", mapping)


def mapping_inv(mapping):
    return {v: k for k, v in mapping.items()}


if __name__ == "__main__":
    test_basic_mask_unmask()
    test_consistent_token_same_value()
    test_consistent_across_fields()
    test_unresolved_placeholder_detection()
    print("OK: all pii_mask tests passed")
