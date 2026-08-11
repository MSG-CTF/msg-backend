from django.shortcuts import render
from apps.common.exceptions import InvalidRequest
from apps.common.permissions import IsAdmin
from apps.common.response import ok
from apps.common.utils import num
from rest_framework.decorators import api_view, permission_classes

from apps.accounts.models import Team

SORT_FIELDS = {
    "score": "-team_score",
    "name": "team_name",
}

DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100


def _page_number(raw, default):
    if raw in (None, ""):
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise InvalidRequest("page 와 size 는 정수여야 합니다")
    if value < 1:
        raise InvalidRequest("page 와 size 는 1 이상이어야 합니다")
    return value


@api_view(["GET"])
@permission_classes([IsAdmin])
def team_list(request):
    search = request.query_params.get("search", "").strip()

    sort = request.query_params.get("sort", "score")
    if sort not in SORT_FIELDS:
        raise InvalidRequest("정렬 기준이 올바르지 않습니다. (score, name 중 선택)")

    page = _page_number(request.query_params.get("page"), 1)
    size = min(_page_number(request.query_params.get("size"), DEFAULT_PAGE_SIZE), MAX_PAGE_SIZE)

    queryset = Team.objects.all()
    if search:
        queryset = queryset.filter(team_name__icontains=search)

    total_count = queryset.count()

    offset = (page - 1) * size
    rows = queryset.order_by(SORT_FIELDS[sort], "team_name")[offset : offset + size]

    teams = [
        {
            "team_id": str(team.team_id),
            "team_name": team.team_name,
            "team_score": num(team.team_score),
            "mileage": team.mileage,
            # 보드 앱이 생기면 team_board_states.position 으로 채운다.
            "board_position_states": None,
            "is_banned": team.is_banned,
        }
        for team in rows
    ]

    return ok({"teams": teams, "total_count": total_count, "page": page, "size": size})
