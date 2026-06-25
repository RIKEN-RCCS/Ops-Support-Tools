"""Triage/followup向けLLMの簡易評価。

実チケットや秘密情報を使わず、代表的なサポートケースで一次トリアージ能力を測る。
API key は llm_client 経由で *_FILE から読むため、このスクリプトは値を表示しない。
"""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass
from typing import Any

import llm_client


@dataclass(frozen=True)
class TriageCase:
    case_id: str
    subject: str
    body: str
    category: str
    requires_environment_knowledge: bool
    requires_runbook: bool
    safe_to_reply_to_user: bool
    difficulty: str
    expected_terms: tuple[str, ...]


CASES: tuple[TriageCase, ...] = (
    TriageCase(
        case_id="cuda_gcc_hpcx",
        subject="CUDA+GCCでHPC-Xを使いたい",
        body=(
            "CUDA+gccのMPI並列プログラムです。この環境にはCUDA+GCC+GCCビルドされた"
            "HPC-Xがないようなのですが、自分でコンパイルしたらよいですか。"
        ),
        category="software",
        requires_environment_knowledge=True,
        requires_runbook=True,
        safe_to_reply_to_user=False,
        difficulty="normal",
        expected_terms=("cuda", "gcc", "hpc-x", "mpi", "module"),
    ),
    TriageCase(
        case_id="quota_cleanup",
        subject="ホーム領域のquota超過",
        body=(
            "ジョブが失敗します。エラーには Disk quota exceeded と出ています。"
            "どのファイルを消せばよいか教えてください。"
        ),
        category="storage",
        requires_environment_knowledge=True,
        requires_runbook=True,
        safe_to_reply_to_user=False,
        difficulty="normal",
        expected_terms=("quota", "disk", "削除", "確認"),
    ),
    TriageCase(
        case_id="password_reset",
        subject="ログインできません",
        body="パスワードを忘れてログインできません。再設定方法を教えてください。",
        category="account",
        requires_environment_knowledge=False,
        requires_runbook=False,
        safe_to_reply_to_user=True,
        difficulty="low",
        expected_terms=("パスワード", "再設定", "ログイン"),
    ),
    TriageCase(
        case_id="ondemand_gpu",
        subject="Open OnDemandのGPUアプリが起動しません",
        body=(
            "WebからGPUアプリを起動するとすぐ終了します。CLIのジョブ投入では動きます。"
            "OnDemand側の設定かmoduleの差分でしょうか。"
        ),
        category="ondemand",
        requires_environment_knowledge=True,
        requires_runbook=True,
        safe_to_reply_to_user=False,
        difficulty="normal",
        expected_terms=("ondemand", "gpu", "module", "ログ", "ジョブ"),
    ),
)


def _textify(value: Any) -> str:
    if isinstance(value, list):
        return "\n".join(_textify(v) for v in value)
    if isinstance(value, dict):
        return "\n".join(_textify(v) for v in value.values())
    return str(value or "")


def _score(case: TriageCase, output: dict[str, Any]) -> tuple[int, list[str]]:
    score = 0
    notes: list[str] = []
    if output.get("category") == case.category:
        score += 25
        notes.append("category=25/25")
    else:
        notes.append(f"category=0/25 got={output.get('category')}")

    gate_keys = (
        "requires_environment_knowledge",
        "requires_runbook",
        "safe_to_reply_to_user",
    )
    matched_gates = sum(1 for key in gate_keys if bool(output.get(key)) == bool(getattr(case, key)))
    gate_score = round(30 * matched_gates / len(gate_keys))
    score += gate_score
    notes.append(f"gates={gate_score}/30 matched={matched_gates}/{len(gate_keys)}")

    if output.get("difficulty") == case.difficulty:
        score += 10
        notes.append("difficulty=10/10")
    else:
        notes.append(f"difficulty=0/10 got={output.get('difficulty')}")

    confidence = output.get("answer_confidence")
    expected_low = case.requires_environment_knowledge or case.requires_runbook
    confidence_ok = confidence == "low" if expected_low else confidence in {"medium", "high"}
    if confidence_ok:
        score += 10
        notes.append("confidence=10/10")
    else:
        notes.append(f"confidence=0/10 got={confidence}")

    text = _textify(output).lower()
    matched_terms = [term for term in case.expected_terms if term.lower() in text]
    term_score = round(15 * len(matched_terms) / len(case.expected_terms))
    score += term_score
    notes.append(f"terms={term_score}/15 matched={','.join(matched_terms)}")

    draft = str(output.get("draft_reply") or "")
    safe = bool(output.get("safe_to_reply_to_user"))
    draft_ok = bool(draft.strip()) and (safe or ("確認" in draft or "お時間" in draft or "保留" in draft))
    if draft_ok:
        score += 10
        notes.append("draft_policy=10/10")
    else:
        notes.append("draft_policy=0/10")
    return score, notes


def _evaluate_case(model: str, case: TriageCase) -> dict[str, Any]:
    start = time.monotonic()
    output = llm_client.triage(case.subject, case.body, model=model)
    elapsed = time.monotonic() - start
    score, notes = _score(case, output)
    return {
        "model": model,
        "case_id": case.case_id,
        "score": score,
        "elapsed_sec": round(elapsed, 2),
        "notes": notes,
        "output": output,
    }


def _parse_models(value: str | None) -> list[str]:
    if value:
        return [model.strip() for model in value.split(",") if model.strip()]
    return llm_client.runbook_model_candidates()


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate triage-oriented LLM behavior.")
    parser.add_argument("--models", help="Comma-separated model names.")
    parser.add_argument("--case", choices=["all"] + [case.case_id for case in CASES], default="all")
    parser.add_argument("--json", action="store_true", help="Print full JSON results including model outputs.")
    args = parser.parse_args()

    models = _parse_models(args.models)
    selected_cases = CASES if args.case == "all" else tuple(case for case in CASES if case.case_id == args.case)
    results: list[dict[str, Any]] = []
    for model in models:
        for case in selected_cases:
            try:
                results.append(_evaluate_case(model, case))
            except Exception as exc:  # noqa: BLE001
                results.append({
                    "model": model,
                    "case_id": case.case_id,
                    "score": 0,
                    "elapsed_sec": None,
                    "notes": [f"error={exc}"],
                    "output": None,
                })

    if args.json:
        print(json.dumps({"results": results}, ensure_ascii=False, indent=2))
        return 0

    by_model: dict[str, list[int]] = {}
    for result in results:
        by_model.setdefault(result["model"], []).append(int(result["score"]))
        print(
            f"{result['model']}\t{result['case_id']}\t"
            f"score={result['score']}\telapsed={result['elapsed_sec']}\t"
            + " | ".join(result["notes"])
        )
    for model, scores in by_model.items():
        avg = round(sum(scores) / len(scores), 1) if scores else 0
        print(f"SUMMARY\t{model}\tavg={avg}\tcases={len(scores)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
