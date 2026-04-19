"""
LLM-as-judge: Claude CLI(`claude -p`)로 path_trace step의 정합성 판정.

배경:
  정규식 휴리스틱은 서술형 explanation/choices를 거의 못 잡아 스킵률 90%.
  → 다른 모델(Claude)이 step을 직접 읽고 explanation 기준으로 정답 인덱스를
    독립 추출. 추출된 인덱스가 step의 correct_index와 다르면 버그 후보.

실행 위치:
  호스트(맥북)에서 실행. `claude` CLI는 호스트에 있고 Docker 안엔 없음.
  프로젝트 루트(/Users/shinhaeran/Desktop/혀란/learn-log)에서:

    python3 search/experiments/llm_judge.py <input.jsonl> [--limit N] [--model MODEL]

  --limit 5 로 dry-run 추천 후 전체 실행.

출력:
  같은 폴더에 judged_<input>.jsonl 로 누적 저장. 이어쓰기 지원(중단해도 안전).
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

JUDGE_PROMPT_TEMPLATE = """객관식 문제의 correct_index가 explanation과 일치하는지 판정해주세요.

질문: {question}

선택지:
{choices_block}

설명(explanation): {explanation}

위 설명만을 근거로, 정답에 해당하는 선택지 인덱스(0~{max_idx})를 골라주세요.
설명이 가리키는 정답과 가장 일치하는 선택지를 고르면 됩니다.

설명이 모호하거나 모순되어 하나의 선택지를 특정할 수 없으면 -1을 반환하세요.

JSON만 출력하세요 (마크다운, 부가 설명 없이):
{{"correct_index": <0~{max_idx} 또는 -1>, "reasoning": "<한국어 1~2문장>", "confidence": "<high|medium|low>"}}
"""


def build_judge_prompt(step):
    question = step.get("question", "")
    choices = step.get("choices", [])
    explanation = step.get("explanation", "")
    choices_block = "\n".join(f"  [{i}] {c}" for i, c in enumerate(choices))
    return JUDGE_PROMPT_TEMPLATE.format(
        question=question,
        choices_block=choices_block,
        explanation=explanation,
        max_idx=max(len(choices) - 1, 0),
    )


def _strip_codeblock(text):
    text = text.strip()
    if text.startswith("```"):
        parts = text.split("```")
        if len(parts) >= 2:
            text = parts[1]
            if text.startswith("json"):
                text = text[4:]
    return text.strip()


def call_claude(prompt, model):
    """claude -p 호출 → 모델 응답 텍스트를 JSON으로 파싱.

    --output-format json 사용 시 Claude Code가 outer envelope을 줌.
    실제 모델 응답은 envelope["result"]에 들어있다.
    """
    try:
        result = subprocess.run(
            ["claude", "-p", prompt, "--model", model, "--output-format", "json"],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        return {"error": "timeout"}
    except FileNotFoundError:
        return {"error": "claude CLI not found in PATH"}

    if result.returncode != 0:
        return {"error": f"non-zero exit ({result.returncode})", "stderr": result.stderr[:300]}

    try:
        envelope = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        return {"error": f"envelope parse fail: {e}", "raw_stdout": result.stdout[:400]}

    inner_text = envelope.get("result")
    if not isinstance(inner_text, str):
        return {"error": "no 'result' field in envelope", "envelope_keys": list(envelope.keys())}

    inner_text = _strip_codeblock(inner_text)

    try:
        return json.loads(inner_text)
    except json.JSONDecodeError as e:
        return {"error": f"verdict parse fail: {e}", "raw_inner": inner_text[:400]}


def already_judged(output_path):
    seen = set()
    if not output_path.exists():
        return seen
    with output_path.open("r", encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line)
                key = (rec.get("label"), rec.get("query_id"),
                       rec.get("iteration"), rec.get("step_idx"))
                seen.add(key)
            except json.JSONDecodeError:
                continue
    return seen


def append_jsonl(path, record):
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path, help="judging할 JSONL 경로")
    parser.add_argument("--limit", type=int, default=None,
                        help="처리할 최대 step 수 (dry-run용)")
    parser.add_argument("--model", default="claude-haiku-4-5-20251001",
                        help="judge 모델 (기본: Haiku 4.5)")
    args = parser.parse_args()

    if not args.input.exists():
        print(f"입력 파일 없음: {args.input}", file=sys.stderr)
        return 1

    output_path = args.input.parent / f"judged_{args.input.name}"
    seen = already_judged(output_path)

    print(f"[judge] 모델:  {args.model}")
    print(f"[judge] 입력:  {args.input}")
    print(f"[judge] 출력:  {output_path}")
    if seen:
        print(f"[judge] 이전 판정 {len(seen)}건 — 이어서 진행")

    # 입력 JSONL → step 단위 task 리스트
    tasks = []
    with args.input.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            parsed = rec.get("parsed")
            if not parsed:
                continue
            for step_idx, step in enumerate(parsed.get("steps", [])):
                key = (rec.get("label"), rec.get("query_id"),
                       rec.get("iteration"), step_idx)
                if key in seen:
                    continue
                tasks.append((key, step))

    if args.limit is not None:
        tasks = tasks[:args.limit]

    print(f"[judge] 판정할 step: {len(tasks)}건\n")

    agree = disagree = ambiguous = errors = 0
    for i, (key, step) in enumerate(tasks, 1):
        label, qid, it, step_idx = key
        original = step.get("correct_index")

        print(f"  [{i}/{len(tasks)}] {label} {qid} iter{it} step{step_idx} "
              f"(orig={original})", flush=True)

        verdict = call_claude(build_judge_prompt(step), model=args.model)

        record = {
            "label": label, "query_id": qid, "iteration": it,
            "step_idx": step_idx,
            "original_correct_index": original,
            "judge_verdict": verdict,
            "step": step,
        }

        if "error" in verdict:
            errors += 1
            print(f"      ⚠️ ERROR: {verdict.get('error')} | {verdict}")
        else:
            judge_idx = verdict.get("correct_index")
            conf = verdict.get("confidence", "?")
            reasoning = (verdict.get("reasoning", "") or "")[:80]
            if judge_idx == -1:
                ambiguous += 1
                tag = "⚠️"
            elif judge_idx == original:
                agree += 1
                tag = "✅"
            else:
                disagree += 1
                tag = "❌"
            record["agreement"] = (judge_idx == original)
            print(f"      {tag} judge={judge_idx} ({conf}) — {reasoning}")

        append_jsonl(output_path, record)

    print(f"\n[judge] 요약 (이번 실행분):")
    print(f"  동의(✅):    {agree}")
    print(f"  불일치(❌):  {disagree}")
    print(f"  모호(⚠️):    {ambiguous}")
    print(f"  에러:        {errors}")
    print(f"\n[judge] 결과: {output_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
