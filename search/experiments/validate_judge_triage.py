"""
Judge 3분법 재검증: 구 이진 판정(0611 judge_cases)과 신 3분법 판정 비교.

배경:
  0611 Claude 교차검증에서 Groq Judge가 무관함(irrelevant)을 모순(contradiction)으로
  오판하는 패턴이 확인됐다 (의심 17건 중 16건이 오판). check_consistency를 3분법
  (supported / no_evidence / contradicted) 스키마로 바꾼 것이 이 오판을 실제로
  줄이는지, 같은 로그들을 신 Judge로 재판정해 전이표로 확인한다.

  주의: 신 판정은 새 운영 경로 그대로라(웹 발췌 3건×800자, 구 판정은 2건×200자)
  스키마 변경과 발췌 확대 효과가 합쳐져 측정된다. 비교 대상은 "구 운영 vs 신 운영".

성공 기준 (콘솔 요약에 자동 판정 출력):
  1. 구 '의심' 케이스 대부분이 no_evidence로 재분류될 것 — 무관을 모순으로
     찍던 오판이 빠져나갈 자리가 생겼는지 (Claude 재판정 기준 오판은 16건이었다)
  2. contradicted로 남는 케이스는 소수여야 하고, 남은 pk는 수동 확인 목록으로 출력
     (진짜 환각 후보 — 0611 기준 #45 등)
  3. 구 '일치' 케이스가 대거 contradicted로 뒤집히지 않을 것 (회귀 없음)

실행 (프로젝트 루트에서, GROQ_API_KEY 필요):
  docker compose exec web python search/experiments/validate_judge_triage.py
  docker compose exec web python search/experiments/validate_judge_triage.py \
      --cases benchmarks/0611/results/judge_cases_130407.jsonl --gap 5

--cases를 생략하면 benchmarks/0611/results/judge_cases_*.jsonl 중 최신 파일을 쓴다.
결과: search/experiments/results/judge_triage_{시각}.jsonl + 콘솔 전이표
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

CALL_GAP_SEC = 2  # groq 무료 티어 분당 한도 완화
OLD_CASES_DIR = Path(__file__).resolve().parents[2] / 'benchmarks/0611/results'
RESULTS_DIR = Path(__file__).parent / 'results'


def load_old_cases(path):
    cases = []
    with open(path, encoding='utf-8') as f:
        for line in f:
            if line.strip():
                cases.append(json.loads(line))
    return cases


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--cases', type=Path, default=None,
                        help='구 판정 JSONL (기본: benchmarks/0611/results의 최신 judge_cases)')
    parser.add_argument('--limit', type=int, default=None)
    parser.add_argument('--gap', type=float, default=CALL_GAP_SEC,
                        help='Groq 호출 간격(초). TPM 한도에 걸리면 5~10으로')
    args = parser.parse_args()

    cases_path = args.cases
    if cases_path is None:
        candidates = sorted(OLD_CASES_DIR.glob('judge_cases_*.jsonl'))
        if not candidates:
            sys.exit(f"구 판정 파일이 없습니다: {OLD_CASES_DIR}/judge_cases_*.jsonl")
        cases_path = candidates[-1]

    old_cases = load_old_cases(cases_path)
    if args.limit:
        old_cases = old_cases[:args.limit]
    print(f">> 구 판정 {len(old_cases)}건 로드: {cases_path}")

    service = LearnlogService()
    RESULTS_DIR.mkdir(exist_ok=True)
    out_path = RESULTS_DIR / f"judge_triage_{datetime.now():%H%M%S}.jsonl"

    # 전이표: (구 판정, 신 verdict) → 건수
    transitions = {}
    remained_contradicted = []   # 성공 기준 2: 수동 확인 후보
    new_contradicted_from_ok = []  # 성공 기준 3: 회귀 후보
    skipped = failed = 0

    with open(out_path, 'w', encoding='utf-8') as f:
        for i, case in enumerate(old_cases, start=1):
            log = LearningLog.objects.prefetch_related('references').filter(pk=case['pk']).first()
            refs = list(log.references.all()) if log else []
            if not refs:
                skipped += 1
                continue
            search_results = {'results': [{'url': r.url, 'content': r.excerpt} for r in refs]}

            time.sleep(args.gap)
            try:
                new = service.check_consistency(log.ai_response, search_results=search_results)
            except Exception as e:
                failed += 1
                print(f"  ? #{case['pk']}: 판정 실패 ({e})")
                continue
            if new is None:
                skipped += 1
                continue

            old_label = '일치' if case['groq_consistent'] else '의심'
            key = (old_label, new['verdict'])
            transitions[key] = transitions.get(key, 0) + 1

            if old_label == '의심' and new['verdict'] == 'contradicted':
                remained_contradicted.append((case['pk'], log.query[:40], new['note']))
            if old_label == '일치' and new['verdict'] == 'contradicted':
                new_contradicted_from_ok.append((case['pk'], log.query[:40], new['note']))

            f.write(json.dumps({
                'pk': case['pk'],
                'query': log.query,
                'old_consistent': case['groq_consistent'],
                'old_note': case.get('groq_note', ''),
                'new_verdict': new['verdict'],
                'new_note': new['note'],
            }, ensure_ascii=False) + '\n')
            print(f"  [{i}/{len(old_cases)}] #{case['pk']}: 구 {old_label} → 신 {new['verdict']}")

    # ── 전이표 + 성공 기준 판정 ──────────────────────────────
    print(f"\n=== 전이표 (구 → 신) ===")
    for old_label in ('일치', '의심'):
        row = {v: transitions.get((old_label, v), 0) for v in ('supported', 'no_evidence', 'contradicted')}
        total = sum(row.values())
        print(f"  구 {old_label} ({total}건): supported {row['supported']} / "
              f"no_evidence {row['no_evidence']} / contradicted {row['contradicted']}")
    print(f"  건너뜀 {skipped} / 실패 {failed}")

    suspect_total = sum(transitions.get(('의심', v), 0)
                        for v in ('supported', 'no_evidence', 'contradicted'))
    moved = suspect_total - len(remained_contradicted)
    print(f"\n=== 성공 기준 ===")
    if suspect_total:
        print(f"  1. 구 의심 {suspect_total}건 중 {moved}건이 모순 아님으로 재분류 "
              f"({moved / suspect_total * 100:.0f}%) — 오판(Claude 기준 16/17건)이 빠졌는지 확인")
    print(f"  2. contradicted 잔류 {len(remained_contradicted)}건 — 진짜 환각 후보, 수동 확인:")
    for pk, query, note in remained_contradicted:
        print(f"     ⚠️ #{pk}: {query} — {note}")
    print(f"  3. 구 일치 → contradicted 반전 {len(new_contradicted_from_ok)}건 (0에 가까울수록 회귀 없음):")
    for pk, query, note in new_contradicted_from_ok:
        print(f"     ⚠️ #{pk}: {query} — {note}")

    print(f"\n상세 결과: {out_path}")
    print("반영하려면: docker compose exec web python manage.py verify_logs --apply")


if __name__ == '__main__':
    main()
