import json

import pytest
from django.urls import reverse

from practice.models import BKTObservation, Concept, ConceptMastery, Exercise, ExerciseAttempt
from practice.services.bkt_report import concept_report
from practice.services.bkt_service import BKTService
from practice.templatetags.bkt_display import percent
from search.tests.factories import LearningLogFactory


pytestmark = pytest.mark.django_db


@pytest.fixture
def approved_exercise():
    concept = Concept.objects.create(id='test.nowait', name='NOWAIT')
    return Exercise.objects.create(
        learning_log=LearningLogFactory(), exercise_type='path_trace',
        content={'scenario': '잠금 경합', 'steps': [{
            'question': '기다리지 않고 오류를 반환하는 옵션은?',
            'choices': ['NOWAIT', 'WAIT', 'SELECT', 'UPDATE'],
            'correct_index': 0, 'correct_answer': 'NOWAIT',
            'explanation': 'NOWAIT는 잠금 대기 대신 오류를 반환한다.',
            'concept_id': concept.pk, 'concept_candidate': None,
            '_concept_review': {'status': 'approved'},
        }]},
    )


def observe(exercise, choice):
    attempt = ExerciseAttempt.objects.create(
        exercise=exercise, user_answer={'selected_indices': [choice]},
        score=int(choice == 0), is_correct=choice == 0,
    )
    BKTService.record_attempt(attempt)
    return attempt.bkt_observations.get()


def test_stats_empty_and_unobserved_concept(client):
    response = client.get(reverse('search:stats'))
    assert response.status_code == 200
    assert '등록된 개념이 없습니다' in response.content.decode()
    Concept.objects.create(id='test.empty', name='관측 전 개념')
    response = client.get(reverse('search:stats'))
    assert '관측 없음' in response.content.decode()
    assert response.context['bkt_points'] == []
    assert not ConceptMastery.objects.exists()


def test_first_observation_draws_initial_and_final_point(client, approved_exercise):
    observation = observe(approved_exercise, 0)
    response = client.get(reverse('search:stats'))
    points = response.context['bkt_points']
    assert response.status_code == 200
    assert len(points) == 2
    assert points[0]['mastery'] == .3
    assert points[0]['x'] == 50
    assert points[1]['x'] == 650
    assert points[1]['mastery'] == observation.mastery_after
    assert points[1]['y'] == pytest.approx(220 - observation.mastery_after * 200)
    html = response.content.decode()
    assert '64.6%' in html
    assert '67.0%' in html
    assert 'bkt-chart-title' in html
    assert '당시 사용한 파라미터' in html


def test_chart_is_time_ordered_and_details_are_concept_scoped(client, approved_exercise):
    first = observe(approved_exercise, 0)
    second = observe(approved_exercise, 1)
    other = Concept.objects.create(id='test.other', name='관측 없음 개념')
    response = client.get(reverse('search:stats'), {
        'concept': first.concept_id, 'observation': str(first.pk),
    })
    assert list(response.context['bkt_observations']) == [first, second]
    assert response.context['bkt_detail'] == first
    assert len(response.context['bkt_points']) == 3
    response = client.get(reverse('search:stats'), {
        'concept': other.pk, 'observation': str(first.pk),
    })
    assert response.context['bkt_detail'] is None
    assert response.context['bkt_points'] == []
    assert BKTObservation.objects.count() == 2


def test_invalid_selection_falls_back_and_read_does_not_modify_state(approved_exercise):
    observation = observe(approved_exercise, 0)
    state = ConceptMastery.objects.values().get()
    report = concept_report('not-a-concept', 'not-an-id')
    assert report['bkt_selected']['concept'].pk == observation.concept_id
    assert report['bkt_detail'] == observation
    assert state == ConceptMastery.objects.values().get()


def test_prediction_uses_stored_parameters_not_current_defaults(approved_exercise):
    observe(approved_exercise, 0)
    state = ConceptMastery.objects.get()
    state.parameters.update(slip=.2, guess=.1)
    state.save()
    report = concept_report()
    assert report['bkt_concepts'][0]['predicted_correct'] == pytest.approx(state.mastery * .8 + (1 - state.mastery) * .1)


def test_result_shows_saved_update_and_parameters(client, approved_exercise):
    response = client.post(reverse('practice:exercise_attempt', args=[approved_exercise.pk]), {
        'selected_indices': json.dumps([0]),
    })
    assert response.status_code == 200
    html = response.content.decode()
    assert '숙련도 변화 · BKT' in html
    assert '풀이 전 30.0% → 풀이 후 64.6%' in html
    assert '정오답 반영 직후 60.7%' in html
    assert '예상 정답률 44.5%' in html
    assert '학습 전이율' in html
    assert BKTObservation.objects.count() == 1


def test_unreviewed_result_explicitly_shows_not_applied(client, approved_exercise):
    approved_exercise.content['steps'][0]['_concept_review']['status'] = 'on_hold'
    approved_exercise.save(update_fields=['content'])
    response = client.post(reverse('practice:exercise_attempt', args=[approved_exercise.pk]), {
        'selected_indices': '[0]',
    })
    assert 'BKT 미반영' in response.content.decode()
    assert not BKTObservation.objects.exists()


def test_percent_preserves_zero():
    assert percent(0) == '0.0%'
    assert percent(1) == '100.0%'
    assert percent(None) == '—'
