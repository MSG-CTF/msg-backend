import math

from apps.challenge.models import Challenge, Solve

def calculate_dynamic_score(initial_score, minimum_score, decay, solved_team_count):
    value = ((minimum_score - initial_score) / (decay ** 2)) * (solved_team_count ** 2) + initial_score
    value = math.ceil(value)

    if value < minimum_score:
        return minimum_score

    return value


def recalculate_challenge_score(challenge_id):
    challenge = Challenge.objects.select_for_update().get(pk=challenge_id)

    solved_team_count = Solve.objects.filter(
        challenge_id=challenge_id,
    ).values("team_id").distinct().count()

    new_score = calculate_dynamic_score(
        challenge.initial_score,
        challenge.minimum_score,
        challenge.decay,
        solved_team_count,
    )

    challenge.current_score = new_score
    challenge.save(update_fields=["current_score"])

    return new_score