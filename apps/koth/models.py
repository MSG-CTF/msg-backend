import secrets
import uuid

from django.db import models


class KothClub(models.Model):
    """ERD: koth_clubs — 동아리 6개. 동아리 하나가 문제를 여러 개 낼 수 있다 (1:N, GET /api/v1/koth/clubs)."""

    club_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=100)

    class Meta:
        db_table = "koth_clubs"

    def __str__(self):
        return self.name


class KothChallengeStatus(models.TextChoices):
    SCHEDULED = "SCHEDULED"
    ACTIVE = "ACTIVE"
    CLOSED = "CLOSED"


class KothChallenge(models.Model):
    """ERD: koth_challenges — 동아리 6개 x 문제 2개, 총 12문제. open_group/status는 동아리가 아니라
    문제 단위로 관리한다 (한 동아리의 문제 두 개가 서로 다른 시간대에 열릴 수 있음).
    """

    koth_challenge_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    club = models.ForeignKey(KothClub, on_delete=models.CASCADE, related_name="challenges")
    title = models.CharField(max_length=100)
    category = models.CharField(max_length=20)
    open_group = models.PositiveSmallIntegerField()
    status = models.CharField(
        max_length=20, choices=KothChallengeStatus.choices, default=KothChallengeStatus.SCHEDULED
    )
    opened_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)

    # /internal/koth/scores 아웃바운드 폴링용. 문제 서버 실제 배포 주소를 아직 자동으로 알아낼
    # 방법이 없어 관리자가 직접 채워 넣는다 (scheduler/runtime 연동은 별도 결정 필요).
    score_api_base_url = models.URLField(null=True, blank=True)
    score_api_internal_token = models.CharField(max_length=255, null=True, blank=True)

    class Meta:
        db_table = "koth_challenges"
        ordering = ["club_id", "title"]

    def __str__(self):
        return f"{self.title} ({self.status})"


class KothScoringPeriod(models.Model):
    """ERD: koth_scoring_periods — 이미 배점을 적용한 (문제, 15분 구간) 조합. 재처리 방지용."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    challenge = models.ForeignKey(KothChallenge, on_delete=models.CASCADE, related_name="scoring_periods")
    period_id = models.DateTimeField()
    raw_response = models.JSONField()
    processed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "koth_scoring_periods"
        constraints = [
            models.UniqueConstraint(fields=["challenge", "period_id"], name="unique_challenge_period"),
        ]
        indexes = [
            models.Index(fields=["challenge", "period_id"]),
        ]

    def __str__(self):
        return f"{self.challenge_id}: {self.period_id}"


def _generate_team_token():
    return f"koth_{secrets.token_urlsafe(24)}"


class KothTeamToken(models.Model):
    """ERD: koth_team_tokens — 참가자가 KOTH 문제 서버 로그인에 쓰는 팀별 토큰. 로그인 JWT와는 별개."""

    team = models.OneToOneField(
        "accounts.Team",
        on_delete=models.CASCADE,
        primary_key=True,
        related_name="koth_token",
    )
    token = models.CharField(max_length=64, unique=True, default=_generate_team_token)
    issued_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "koth_team_tokens"

    def __str__(self):
        return str(self.team_id)


class KothSolve(models.Model):
    """ERD: koth_solves — 15분 채점에서 처음 양수 점수를 받은 시점에 생성, 이후 점수는 누적."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    team = models.ForeignKey("accounts.Team", on_delete=models.CASCADE, related_name="koth_solves")
    challenge = models.ForeignKey(KothChallenge, on_delete=models.CASCADE, related_name="solves")
    earned_score = models.PositiveIntegerField(default=0)
    solved_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "koth_solves"
        constraints = [
            models.UniqueConstraint(fields=["team", "challenge"], name="unique_team_koth_challenge_solve"),
        ]
        indexes = [
            models.Index(fields=["challenge", "-earned_score"]),
        ]

    def __str__(self):
        return f"{self.team_id}: {self.challenge_id} ({self.earned_score})"
