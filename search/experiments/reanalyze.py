"""
기존 JSONL을 개선된 휴리스틱으로 재분석 (API 호출 0).

목적:
  - baseline_*.jsonl / regression_*.jsonl에 저장된 Groq 응답을 다시 읽어
    개선된 check_step으로 오류율을 재계산한다.
  - 최초 baseline은 첫 매칭 숫자 추출 휴리스틱의 false positive로
    실제보다 오류율이 과대 평가됐음 (약 75% false positive 확인됨).

실행:
  python -m search.experiments.reanalyze <jsonl_path>
  python -m search.experiments.reanalyze  # 인자 없으면 results/의 최신 jsonl 자동 선택
"""

import json
import sys
from collections import defaultdict
from pathlib import Path

from search.experiments._common import RESULTS_DIR, check_step


def load_jsonl(path):
    records = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            records.append(json.loads(line))
    return records


def _bucket_skip_reason(detail):
    """check_step의 detail 문자열을 스킵 사유 카테고리로 분류."""
    if "구조 불완전" in detail:
        return "structure_incomplete"
    if "정답 추출 불가" in detail:
        return "extract_none"
    if "판단 보류" in detail:
        return "multiple_match"
    if "어떤 choice에도 명확히 없음" in detail:
        return "no_match_in_choices"
    return "other"


def latest_jsonl():
    candidates = sorted(RESULTS_DIR.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
    return candidates[-1] if candidates else None


def reanalyze(records):
    """label별(OLD/NEW) 통계 집계."""
    by_label = defaultdict(lambda: {
        "calls": 0,
        "parse_failures": 0,
        "total_steps": 0,
        "verified_steps": 0,
        "error_steps": 0,
        "skipped_steps": 0,
        "skip_reasons": defaultdict(int),
        "errors_detail": [],
        "by_query": defaultdict(lambda: {
            "verified_steps": 0, "error_steps": 0, "total_steps": 0,
        }),
    })

    for rec in records:
        label = rec.get("label", "?")
        qid = rec.get("query_id", "?")
        it = rec.get("iteration")
        parsed = rec.get("parsed")

        bl = by_label[label]
        bl["calls"] += 1

        if not parsed:
            bl["parse_failures"] += 1
            continue

        for step_idx, step in enumerate(parsed.get("steps", [])):
            bl["total_steps"] += 1
            bl["by_query"][qid]["total_steps"] += 1

            check = check_step(step)
            if check.get("strategy") == "skipped":
                bl["skipped_steps"] += 1
                reason = _bucket_skip_reason(check.get("detail", ""))
                bl["skip_reasons"][reason] += 1
                continue

            bl["verified_steps"] += 1
            bl["by_query"][qid]["verified_steps"] += 1

            if check["has_error"]:
                bl["error_steps"] += 1
                bl["by_query"][qid]["error_steps"] += 1
                bl["errors_detail"].append(
                    f"{label} {qid} iter{it} step{step_idx} "
                    f"[{check['strategy']}]: {check['detail']}"
                )

    return {k: v for k, v in by_label.items()}


def print_report(stats):
    print("=" * 72)
    print("  재분석 리포트 (개선된 휴리스틱 적용)")
    print("=" * 72)

    for label in sorted(stats.keys()):
        s = stats[label]
        v = s["verified_steps"]
        rate = (s["error_steps"] / v) if v > 0 else 0.0
        print(f"\n  [{label}]")
        print(f"   호출:        {s['calls']}")
        print(f"   파싱 실패:   {s['parse_failures']}")
        print(f"   총 step:     {s['total_steps']}")
        print(f"   검증된 step: {v}  (스킵 {s['skipped_steps']})")
        print(f"   오류 step:   {s['error_steps']}")
        print(f"   오류율:      {rate:.1%}" if v > 0 else "   오류율:      N/A")

        if s["skip_reasons"]:
            print("   스킵 사유:")
            for reason, count in sorted(s["skip_reasons"].items(), key=lambda x: -x[1]):
                pct = count / s["skipped_steps"] if s["skipped_steps"] else 0
                print(f"     {reason:22s}: {count:3d}  ({pct:.0%})")

        if s["by_query"]:
            print("   쿼리별:")
            for qid, q in s["by_query"].items():
                qv = q["verified_steps"]
                qr = (q["error_steps"] / qv) if qv > 0 else 0.0
                tag = f"{qr:.0%}" if qv > 0 else "N/A"
                print(f"     {qid:20s}: {q['error_steps']}/{qv} ({tag})")

        if s["errors_detail"]:
            print("   오류 상세:")
            for d in s["errors_detail"]:
                print(f"     - {d}")

    # OLD vs NEW 비교
    if "OLD" in stats and "NEW" in stats:
        old_v = stats["OLD"]["verified_steps"]
        new_v = stats["NEW"]["verified_steps"]
        old_r = (stats["OLD"]["error_steps"] / old_v) if old_v > 0 else 0.0
        new_r = (stats["NEW"]["error_steps"] / new_v) if new_v > 0 else 0.0
        print("\n" + "─" * 72)
        print(f"  OLD 오류율: {old_r:.1%}  ({stats['OLD']['error_steps']}/{old_v})")
        print(f"  NEW 오류율: {new_r:.1%}  ({stats['NEW']['error_steps']}/{new_v})")
        diff = old_r - new_r
        if diff > 0:
            print(f"  개선폭:     {diff:+.1%}p (NEW가 오류 감소)")
        elif diff < 0:
            print(f"  개선폭:     {diff:+.1%}p (NEW가 오히려 오류 증가)")
        else:
            print(f"  개선폭:     0 (차이 없음)")
    print("=" * 72)


def main():
    if len(sys.argv) > 1:
        path = Path(sys.argv[1])
    else:
        path = latest_jsonl()
        if path is None:
            print(f"[reanalyze] {RESULTS_DIR}에 jsonl 파일이 없습니다.")
            return 1
        print(f"[reanalyze] 최신 파일 자동 선택: {path.name}")

    if not path.exists():
        print(f"[reanalyze] 파일 없음: {path}")
        return 1

    records = load_jsonl(path)
    print(f"[reanalyze] {len(records)}개 레코드 로드")

    stats = reanalyze(records)
    print_report(stats)
    return 0


if __name__ == "__main__":
    sys.exit(main())
