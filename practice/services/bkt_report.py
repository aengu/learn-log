from dataclasses import asdict

from ..models import BKTObservation, Concept
from .bkt_math import DEFAULT_PARAMETERS


def concept_report(concept_id=None, observation_id=None):
    """저장된 파라미터/숙련도를 읽기만 하며, 선택 개념의 SVG 좌표를 만든다."""
    rows = []
    for concept in Concept.objects.select_related('user_mastery').order_by('name', 'pk'):
        state = getattr(concept, 'user_mastery', None)
        predicted = None
        if state:
            predicted = state.mastery * (1 - state.parameters['slip']) + (1 - state.mastery) * state.parameters['guess']
        rows.append({'concept': concept, 'state': state, 'predicted_correct': predicted})
    selected = next((row for row in rows if row['concept'].pk == concept_id), rows[0] if rows else None)
    observations = list(BKTObservation.objects.filter(
        concept=selected['concept'],
    ).select_related('attempt').order_by('observed_at', 'attempt_id', 'step_index')) if selected else []
    points = []
    if observations:
        points.append({'x': 50, 'y': 220 - observations[0].mastery_before * 200,
                       'mastery': observations[0].mastery_before, 'number': 0})
        for number, observation in enumerate(observations, 1):
            points.append({'x': 50 + number / len(observations) * 600,
                           'y': 220 - observation.mastery_after * 200,
                           'mastery': observation.mastery_after, 'number': number,
                           'observation': observation})
    detail = next((obs for obs in observations if str(obs.pk) == observation_id), observations[-1] if observations else None)
    return {
        'bkt_concepts': rows,
        'bkt_selected': selected,
        'bkt_points': points,
        'bkt_polyline': ' '.join(f"{point['x']:.2f},{point['y']:.2f}" for point in points),
        'bkt_observations': observations,
        'bkt_detail': detail,
        'bkt_parameters': asdict(DEFAULT_PARAMETERS),
    }
