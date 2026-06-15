"""
Judge 교차검증 2단계: Claude 재판정 + 일치율 (호스트에서 실행)

1단계(export_judge_cases.py)가 내보낸 케이스를 Claude가 **같은 프롬프트**로
재판정하고 Groq Judge와의 일치율을 잰다. 프롬프트·컨텍스트·답변이 동일하므로
판정이 갈리면 모델 차이다. 불일치 케이스는 사람이 직접 보고 어느 쪽이 맞는지
가리면 된다 — 그게 Judge 품질의 최종 평가 데이터가 된다.

Claude 호출은 claude CLI 헤드리스 모드(claude -p)를 사용한다.
API 키·추가 비용 없이 Claude Code 구독으로 돈다. 호출당 수 초씩 걸리므로
전체(~90건)는 10~20분 — 먼저 --limit 10으로 맛보기 추천.

실행:
  python3 benchmarks/0611/benchmark_judge_crosscheck.py benchmarks/0611/results/judge_cases_XXXXXX.jsonl
  옵션: --limit N (앞 N건만), --model opus|sonnet|haiku (기본: claude 기본 모델)
"""
import argparse
import json
import subprocess
from datetime import datetime
from pathlib import Path

# Groq Judge(check_consistency)와 동일한 프롬프트 — 모델만 다르게
JUDGE_PROMPT = """AI 답변이 생성에 사용된 참고 컨텍스트와 모순되는지 검사하세요.

참고 컨텍스트:
{context}

답변:
{answer}

JSON으로만 응답하세요 (```없이):
{{
  "consistent": true/false,
  "note": "모순이 있으면 어떤 주장이 어긋나는지 한 문장 (없으면 빈 문자열)"
}}

판단 기준:
- 답변의 주장(수치, 동작 설명, API 사용법)이 컨텍스트 내용과 명백히 어긋나면 consistent=false
- 컨텍스트에 없는 내용을 답변이 추가로 다루는 것은 모순이 아님"""


def ask_claude(prompt, model=None):
    """claude -p 헤드리스 호출 → JSON 판정 파싱"""
    cmd = ['claude', '-p', prompt, '--output-format', 'json']
    if model:
        cmd += ['--model', model]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip()[:200] or 'claude CLI 실행 실패')

    envelope = json.loads(proc.stdout)
    raw = envelope['result'].strip()
    if raw.startswith('```'):  # 코드펜스 방어 (Groq 파서와 동일)
        parts = raw.split('```')
        raw = parts[1] if len(parts) > 1 else raw
        if raw.startswith('json'):
            raw = raw[4:]
    return json.loads(raw.strip())


def dedupe(cases):
    """
    같은 질문(query)의 중복 케이스 제거 — 테스트하며 반복 질문한 로그들이라
    판정 정보가 거의 같은데 Claude 호출만 소모한다.
    단, 그룹 안에 Groq이 의심(⚠️)한 케이스가 있으면 그걸 남긴다 (정보량 최대).
    """
    groups = {}
    for case in cases:
        key = case['query'].strip()
        kept = groups.get(key)
        if kept is None or (kept['groq_consistent'] and not case['groq_consistent']):
            groups[key] = case
    deduped = sorted(groups.values(), key=lambda c: c['pk'])
    print(f"중복 제거: {len(cases)}건 → {len(deduped)}건")
    return deduped


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('cases_file', help='1단계가 만든 judge_cases_*.jsonl 경로')
    parser.add_argument('--limit', type=int, default=None)
    parser.add_argument('--model', default=None, help='opus | sonnet | haiku (기본: claude 설정값)')
    parser.add_argument('--dedupe', action='store_true', help='같은 질문의 중복 케이스 제거')
    parser.add_argument('--skip', type=int, default=0,
                        help='앞 N건 건너뛰기 (이미 판정한 분량 이어서 돌릴 때 — dedupe 후 기준)')
    args = parser.parse_args()

    cases = [json.loads(line) for line in open(args.cases_file, encoding='utf-8')]
    if args.dedupe:
        cases = dedupe(cases)
    if args.skip:
        cases = cases[args.skip:]
        print(f"앞 {args.skip}건 건너뜀 → {len(cases)}건 진행")
    if args.limit:
        cases = cases[:args.limit]

    results = []
    agree = disagree = failed = 0

    # 한 건 판정될 때마다 즉시 기록 — 중간에 끊어도(Ctrl+C, 사용량 한도) 결과가 남는다
    out_path = Path(args.cases_file).parent / f"judge_crosscheck_{datetime.now():%H%M%S}.jsonl"
    out_file = open(out_path, 'w', encoding='utf-8')
    try:
        for i, case in enumerate(cases, start=1):
            prompt = JUDGE_PROMPT.format(context=case['context'], answer=case['answer'])
            try:
                verdict = ask_claude(prompt, model=args.model)
                claude_consistent = bool(verdict.get('consistent', True))
                claude_note = verdict.get('note', '')
            except KeyboardInterrupt:
                raise
            except Exception as e:
                failed += 1
                print(f"  ? #{case['pk']}: Claude 판정 실패 ({e})")
                continue

            match = claude_consistent == case['groq_consistent']
            agree += match
            disagree += not match
            record = {
                **case,
                'claude_consistent': claude_consistent,
                'claude_note': claude_note,
                'match': match,
            }
            results.append(record)
            out_file.write(json.dumps(record, ensure_ascii=False) + '\n')
            out_file.flush()
            g = '✅' if case['groq_consistent'] else '⚠️'
            c = '✅' if claude_consistent else '⚠️'
            flag = '' if match else '  ← 불일치!'
            print(f"  [{i}/{len(cases)}] #{case['pk']}: Groq {g} / Claude {c}{flag}")
    except KeyboardInterrupt:
        print(f"\n중단됨 — 여기까지 {len(results)}건은 저장돼 있음: {out_path}")
        print(f"이어서: --skip {args.skip + len(results)} (dedupe 기준)")
    finally:
        out_file.close()

    total = agree + disagree
    if total == 0:
        print("\n판정된 케이스가 없습니다.")
        return

    print("\n=== 요약 ===")
    print(f"일치: {agree}/{total} ({agree / total * 100:.0f}%)  불일치: {disagree}  실패: {failed}")

    # 누가 무엇을 의심했나
    both = sum(1 for r in results if not r['groq_consistent'] and not r['claude_consistent'])
    groq_only = sum(1 for r in results if not r['groq_consistent'] and r['claude_consistent'])
    claude_only = sum(1 for r in results if r['groq_consistent'] and not r['claude_consistent'])
    print(f"둘 다 의심: {both}건 (진짜 환각 후보) / Groq만 의심: {groq_only}건 (Groq 오판 후보) / "
          f"Claude만 의심: {claude_only}건 (Groq이 놓친 후보)")

    suspects = [r for r in results if not r['match'] or not r['claude_consistent']]
    if suspects:
        print(f"\n=== 사람이 볼 케이스 ({len(suspects)}건) ===")
        for r in suspects:
            print(f"#{r['pk']}: {r['query'][:50]}")
            print(f"   Groq:   {'일치' if r['groq_consistent'] else '의심 — ' + r['groq_note']}")
            print(f"   Claude: {'일치' if r['claude_consistent'] else '의심 — ' + r['claude_note']}")
    print(f"\n원시 결과 저장: {out_path}")


if __name__ == "__main__":
    main()
