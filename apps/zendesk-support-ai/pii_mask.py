"""PII マスキング(spec §5)。

- allowlist 抽出: subject と コメント body(plain_body 優先)のみ。
- 一貫プレースホルダ置換: 同一の値は常に同一トークン。
- unmask: 出力に残ったプレースホルダをローカルで復元。
- has_unresolved_placeholders: 対応表に無いプレースホルダ(LLM の捏造)を検出。

正規表現では日本語の人名は拾えない。機械的に特定可能な識別子を確実に潰す方針(spec §5)。
"""

from __future__ import annotations

import re
from typing import Dict, List, Tuple

# ラベルと検出パターン。順序が優先順位(先に当たったものを優先)。
# IP はメールより先に処理する必要はないが、メールのドメインを IP と誤認しないよう
# メールを先に潰してから IP を見る。
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_IPV4_RE = re.compile(
    r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b"
)
# 電話番号(日本国内/国際のゆるい形。区切りは - . スペース、() を許容)
_PHONE_RE = re.compile(
    r"(?<![\w-])(?:\+?\d{1,3}[-.\s]?)?(?:\(?\d{2,4}\)?[-.\s]?)\d{2,4}[-.\s]?\d{3,4}(?![\w-])"
)
# /home/<user> 中のユーザー名
_HOMEPATH_RE = re.compile(r"(/home/)([A-Za-z0-9_][A-Za-z0-9_.-]*)")
# アカウント ID(spec §10: 実 ID 形式に合わせて要調整。暫定 u\d{5})
_ACCOUNT_RE = re.compile(r"\bu\d{5}\b")

MaskMapping = Dict[str, str]


def _make_masker():
    """ラベルごとのカウンタと逆引きを持つクロージャ群を返す。"""
    counters: Dict[str, int] = {}
    value_to_token: Dict[str, str] = {}

    def token_for(label: str, value: str) -> str:
        # 同一値は常に同一トークン(値で冪等化)
        if value in value_to_token:
            return value_to_token[value]
        counters[label] = counters.get(label, 0) + 1
        tok = f"[{label}_{counters[label]}]"
        value_to_token[value] = tok
        return tok

    return token_for, value_to_token


def mask_fields(fields: List[str]) -> Tuple[List[str], MaskMapping]:
    """複数フィールドを一貫したマッピングでまとめてマスクする。

    フィールド横断で同一値が同一トークンになるよう、単一のマスカ状態を共有する。
    """
    token_for, _ = _make_masker()
    mapping: MaskMapping = {}

    def sub_simple(pattern: re.Pattern, label: str, s: str) -> str:
        def repl(m: re.Match) -> str:
            value = m.group(0)
            tok = token_for(label, value)
            mapping[tok] = value
            return tok
        return pattern.sub(repl, s)

    def home_repl(m: re.Match) -> str:
        value = m.group(2)
        tok = token_for("USER", value)
        mapping[tok] = value
        return m.group(1) + tok

    def mask_one(text: str) -> str:
        if not text:
            return text
        # 順序重要: EMAIL を先に潰してから IP / PHONE を見る
        text = sub_simple(_EMAIL_RE, "EMAIL", text)
        text = sub_simple(_IPV4_RE, "IP", text)
        # /home/<user> はパス構造を保ちユーザー名のみ置換
        text = _HOMEPATH_RE.sub(home_repl, text)
        text = sub_simple(_ACCOUNT_RE, "ACCOUNT", text)
        text = sub_simple(_PHONE_RE, "PHONE", text)
        return text

    return [mask_one(f) for f in fields], mapping


def mask_text(text: str) -> Tuple[str, MaskMapping]:
    """単一テキストをマスクし、(マスク済みテキスト, {token: 元の値}) を返す。"""
    masked, mapping = mask_fields([text])
    return masked[0], mapping


_PLACEHOLDER_RE = re.compile(r"\[[A-Z]+_\d+\]")


def unmask(text: str, mapping: MaskMapping) -> str:
    """プレースホルダを元の値へ復元する。"""
    if not text:
        return text

    def repl(m: re.Match) -> str:
        tok = m.group(0)
        return mapping.get(tok, tok)
    return _PLACEHOLDER_RE.sub(repl, text)


def has_unresolved_placeholders(text: str, mapping: MaskMapping) -> bool:
    """対応表に無いプレースホルダ(LLM の捏造)が残っているか。"""
    for m in _PLACEHOLDER_RE.finditer(text or ""):
        if m.group(0) not in mapping:
            return True
    return False
