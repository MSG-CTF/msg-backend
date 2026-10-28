from apps.common.exceptions import TeamBanned, UserHasNoTeam


def check_active_team(team):
    """잠금으로 읽은 팀 행이 삭제됐거나 정지됐으면 쓰기를 거절한다."""
    if team is None:
        raise UserHasNoTeam()
    if team.is_banned:
        raise TeamBanned()
    return team


def lock_active_team(team_id):
    """참가자 쓰기 요청이 잠금을 잡은 뒤 팀 정지·삭제를 다시 확인한다.

    정지 검사는 인증 단계에서 한 번뿐이라, 그 뒤 정지가 커밋돼도 진행 중인 요청은 끝까지 처리됐다.
    관리자 정지와 같은 팀 행을 잠그므로 둘 중 먼저 잡은 쪽이 끝난 뒤 다른 쪽이 진행된다.
    """
    from apps.accounts.models import Team

    return check_active_team(
        Team.objects.select_for_update(no_key=True).filter(pk=team_id).first()
    )
