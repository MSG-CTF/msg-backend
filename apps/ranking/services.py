from decimal import Decimal

from django.db import IntegrityError, transaction

from apps.board.models import TeamChallengeAccess
from apps.ranking.models import LineMonopoly


LINE_BONUS_SCORE = Decimal("300.00")


def check_and_record_line_monopoly(team, challenge=None, *, line_number=None):
    if line_number is None:
        access = (
            TeamChallengeAccess.objects.filter(team=team, challenge=challenge)
            .select_related("source_cell")
            .first()
        )
        if access is None or access.source_cell.line_number is None:
            return None
        line_number = access.source_cell.line_number

    from apps.board.services import is_line_monopoly_completed

    if not is_line_monopoly_completed(team, line_number):
        return None

    try:
        # The savepoint keeps a duplicate-award IntegrityError from breaking
        # the surrounding correct-submission transaction.
        with transaction.atomic():
            monopoly = LineMonopoly.objects.create(
                team=team,
                line_number=line_number,
                earned_score=LINE_BONUS_SCORE,
                is_category_bonus=False,
            )
    except IntegrityError:
        return None

    return monopoly
