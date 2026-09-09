"""measure_v3_cost가 남긴 JSONL을 모아 v2 vs v3 비용을 다시 계산한다.

측정을 다시 돌리지 않고 기존 기록만 재분석한다(reanalyze.py와 같은 방침).
무료 티어라 한 번에 완주하지 못하고 여러 번 나눠 찍히기 때문에,
파일 여러 개를 합쳐서 봐야 표본이 쓸 만해진다.

지연에는 API 대기열이 섞인다. 레이트 리밋 직후의 호출은 몇십 초씩 튀는데
이건 프롬프트 길이와 무관하므로, 중앙값과 함께 '튄 값 제외' 통계도 같이 낸다.

실행:
  docker compose exec web python -m search.experiments.analyze_v3_cost [파일...]
  (인자가 없으면 results/v3_cost_*.jsonl 전부)
"""

import json
import statistics
import sys
from pathlib import Path

from search.experiments._common import RESULTS_DIR

OUTLIER_FACTOR = 3.0   # 중앙값의 N배를 넘으면 대기열 오염으로 보고 따로 센다


def load(paths):
    rows = []
    for p in paths:
        for line in Path(p).read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
    return [r for r in rows if not r.get("error")]


def split_outliers(values):
    if len(values) < 3:
        return values, []
    med = statistics.median(values)
    keep = [v for v in values if v <= med * OUTLIER_FACTOR]
    drop = [v for v in values if v > med * OUTLIER_FACTOR]
    return keep, drop


def col(rows, key):
    return [r[key] for r in rows if r.get(key) is not None]


def main():
    paths = sys.argv[1:] or sorted(RESULTS_DIR.glob("v3_cost_*.jsonl"))
    if not paths:
        print("분석할 파일이 없다."); return 1

    rows = load(paths)
    print(f"파일 {len(paths)}개 / 성공 호출 {len(rows)}건")
    for p in paths:
        print(f"  · {Path(p).name}")

    groups = {v: [r for r in rows if r["variant"] == v] for v in ("v2", "v3")}
    if not all(groups.values()):
        print("\n한쪽 변형의 표본이 없어 비교 불가."); return 1

    print(f"\n  {'항목':<12}{'v2':>12}{'v3':>12}{'차이':>14}")
    print("  " + "-" * 50)

    def line(label, a_vals, b_vals, fmt="{:.0f}"):
        if not (a_vals and b_vals):
            return
        a, b = statistics.median(a_vals), statistics.median(b_vals)
        ratio = f" ({b / a:.2f}배)" if a else ""
        print(f"  {label:<12}{fmt.format(a):>12}{fmt.format(b):>12}"
              f"{fmt.format(b - a) + ratio:>14}")

    lat2, out2 = split_outliers(col(groups["v2"], "elapsed_sec"))
    lat3, out3 = split_outliers(col(groups["v3"], "elapsed_sec"))

    line("입력토큰", col(groups["v2"], "prompt_tokens"), col(groups["v3"], "prompt_tokens"))
    line("출력토큰", col(groups["v2"], "completion_tokens"), col(groups["v3"], "completion_tokens"))
    line("step수", col(groups["v2"], "steps"), col(groups["v3"], "steps"), "{:.1f}")
    line("지연(초)", lat2, lat3, "{:.2f}")

    print(f"\n  표본:      v2 n={len(groups['v2'])}  v3 n={len(groups['v3'])}")
    print(f"  지연 원자료: v2 {sorted(round(v,2) for v in lat2)}")
    print(f"              v3 {sorted(round(v,2) for v in lat3)}")
    if out2 or out3:
        print(f"  제외한 값:  v2 {[round(v,1) for v in out2]}  v3 {[round(v,1) for v in out3]}"
              f"  ← 레이트 리밋 직후 대기열 오염")

    # 입력 토큰은 프롬프트가 고정이라 매번 같은 값이어야 한다. 흔들리면 측정이 틀린 것이다.
    for v in ("v2", "v3"):
        uniq = set(col(groups[v], "prompt_tokens"))
        if len(uniq) > 1:
            print(f"  ! {v} 입력토큰이 호출마다 다르다({sorted(uniq)}) — 쿼리별 차이 확인 필요")

    print("\n  입력 토큰은 프롬프트가 고정이라 결정적이다. 지연/출력은 표본이 적어 방향만 본다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
