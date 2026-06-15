"""
Judge 교차검증 1단계: 판정 케이스 내보내기 (컨테이너에서 실행)

기존 LearningLog를 reference 발췌와 대조해 Groq Judge(check_consistency)로
판정하고, Claude가 같은 케이스를 재판정할 수 있도록 "컨텍스트 + 답변 + Groq 판정"을
JSONL로 저장한다. 컨텍스트는 운영 검증과 동일한 방식(reference 상위 2건 × 200자)으로
구성해서 두 Judge가 정확히 같은 것을 보게 한다.

실행: docker compose exec web python benchmarks/0611/export_judge_cases.py [--limit N]
출력: benchmarks/0611/results/judge_cases_{시각}.jsonl
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
import django  # noqa: E402

django.setup()

from search.models import LearningLog  # noqa: E402
from search.services import LearnlogService  # noqa: E402

CALL_GAP_SEC = 2
RESULTS_DIR = Path(__file__).parent / "results"


def build_context(refs):
    """운영 check_consistency와 동일한 컨텍스트 구성 (상위 2건 × 200자)"""
    return "\n".join(
        f"[{r.url}] {r.excerpt[:200]}"
        for r in refs[:2]
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--limit', type=int, default=None)
    parser.add_argument('--from-pk', type=int, default=None,
                        help='이 pk 이후부터 재개 (레이트리밋으로 끊겼을 때 — 마지막 성공 pk를 넣으면 됨)')
    parser.add_argument('--gap', type=float, default=CALL_GAP_SEC,
                        help='Groq 호출 간격(초). TPM 한도에 걸리면 5~10으로 늘려서 재시도')
    args = parser.parse_args()

    service = LearnlogService()
    logs = LearningLog.objects.prefetch_related('references').order_by('pk')
    if args.from_pk:
        logs = logs.filter(pk__gt=args.from_pk)
    if args.limit:
        logs = logs[:args.limit]

    RESULTS_DIR.mkdir(exist_ok=True)
    out_path = RESULTS_DIR / f"judge_cases_{datetime.now():%H%M%S}.jsonl"
    exported = skipped = 0

    with open(out_path, "w", encoding="utf-8") as f:
        for log in logs:
            refs = list(log.references.all())
            if not refs:
                skipped += 1
                continue

            search_results = {'results': [{'url': r.url, 'content': r.excerpt} for r in refs]}
            time.sleep(args.gap)
            try:
                verdict = service.check_consistency(log.ai_response, search_results=search_results)
            except Exception as e:
                print(f"  ? #{log.pk}: Groq 판정 실패 ({e})")
                continue
            if verdict is None:
                skipped += 1
                continue

            f.write(json.dumps({
                'pk': log.pk,
                'query': log.query,
                'context': build_context(refs),
                'answer': log.ai_response[:3000],  # Judge가 보는 범위와 동일
                'groq_consistent': verdict['consistent'],
                'groq_note': verdict['note'],
            }, ensure_ascii=False) + "\n")
            exported += 1
            mark = '✅' if verdict['consistent'] else '⚠️'
            print(f"  {mark} #{log.pk}: {log.query[:45]}")

    print(f"\n완료: 내보내기 {exported}건, 건너뜀 {skipped}건 (reference 없음)")
    print(f"케이스 파일: {out_path}")
    print("다음 단계: python3 benchmarks/0611/benchmark_judge_crosscheck.py <케이스 파일>")


if __name__ == "__main__":
    main()
