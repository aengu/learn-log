"""망각 없는 기본 BKT. 파라미터 학습이 아닌 고정 가정값 기반의 상태 추론이다.

수식: https://educationaldatamining.org/EDM2021/virtual/static/pdf/EDM21_paper_237.pdf
"""
from dataclasses import asdict, dataclass
import math


MODEL_VERSION = 'bkt-v1-assumed'


def _validate_probability(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError('확률은 0~1 사이의 유한한 수여야 합니다.')


@dataclass(frozen=True)
class BKTParameters:
    initial_mastery: float = 0.3
    slip: float = 0.1
    guess: float = 0.25
    learn: float = 0.1

    def __post_init__(self):
        for value in asdict(self).values():
            _validate_probability(value)
        if self.slip + self.guess >= 1:
            raise ValueError('알고 있을 때의 정답 확률이 모를 때보다 커야 합니다.')


DEFAULT_PARAMETERS = BKTParameters()


def update_mastery(prior, is_correct, parameters=DEFAULT_PARAMETERS):
    """관측 전 정답 예측 → 베이즈 사후 확률 → 학습 전이 순서로 계산한다."""
    _validate_probability(prior)
    if type(is_correct) is not bool:
        raise ValueError('정오답은 bool이어야 합니다.')
    # DB 변경 없는 순수 계산 1: 관측 전 숙련도에서 이번 문항의 예상 정답 확률을 구한다.
    predicted_correct = prior * (1 - parameters.slip) + (1 - prior) * parameters.guess
    # 계산 2: 실제 정오답이 알려졌을 때, 알고/모르는 상태에서 그 결과가 나올 가능도.
    known_likelihood = 1 - parameters.slip if is_correct else parameters.slip
    unknown_likelihood = parameters.guess if is_correct else 1 - parameters.guess
    evidence = prior * known_likelihood + (1 - prior) * unknown_likelihood
    if evidence == 0:
        raise ValueError('이 파라미터에서 발생할 수 없는 관측입니다.')
    posterior = prior * known_likelihood / evidence
    # 계산 3: 정오답으로 보정한 숙련도에 학습 전이를 반영한다.
    # learn을 그대로 더하지 않고, 아직 모를 확률 (1-posterior)에만 적용한다.
    return {
        'mastery_before': prior,
        'predicted_correct': predicted_correct,
        'mastery_posterior': posterior,
        'mastery_after': posterior + (1 - posterior) * parameters.learn,
    }
