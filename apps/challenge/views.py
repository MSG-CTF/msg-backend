from datetime import timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework.views import APIView

from apps.challenge.models import (
    Challenge,
    FlagSubmission,
    FlagSubmissionLock,
    OpenedChallenge,
    Solve,
)
from apps.challenge.services import hash_flag, is_correct_flag
from apps.common.response import fail, ok
from apps.instances.models import Instance
from apps.instances.services import ACTIVE_INSTANCE_STATUSES, isoformat_z, serialize_instance
from apps.common.permissions import IsAuthenticated

def number_value(value):
    # Decimal 점수를 API 응답용 숫자로 바꾼다
    if value is None:
        return None

    return int(value) if value == int(value) else float(value)


class ChallengeDetailView(APIView):
    permission_classes = [IsAuthenticated]
    def get(self, request, challenge_id):
        team = request.user.team
        if team is None:
            return fail("USER_HAS_NO_TEAM", "소속된 팀이 없습니다", 404)

        challenge = Challenge.objects.filter(challenge_id=challenge_id).first()
        if challenge is None:
            return fail("CHALLENGE_NOT_FOUND", "존재하지 않는 문제 ID입니다.", 404)
        opened_challenge = OpenedChallenge.objects.filter(team=team, challenge=challenge).first()
        if opened_challenge is None:
            return fail("CHALLENGE_LOCKED", "아직 개방되지 않은 문제입니다.", 403)

        solve = Solve.objects.filter(team=team, challenge=challenge).first()
        instance = (
            Instance.objects
            .filter(user=request.user, challenge=challenge, status__in=ACTIVE_INSTANCE_STATUSES)
            .order_by("-created_at")
            .first()
        )

        return ok(
            message="문제 상세 조회 성공",
            data={
                "challenge_id": str(challenge.challenge_id),
                "title": challenge.title,
                "category": challenge.category,
                "difficulty": challenge.difficulty,
                "score": number_value(challenge.score),
                "description": challenge.description,
                "files": [],
                "solved_team_count": Solve.objects.filter(challenge=challenge).count(),
                "is_solved": solve is not None,
                "instance": serialize_instance(instance),
            },
        )


class ChallengeSubmitView(APIView):
    permission_classes = [IsAuthenticated]
    def post(self, request, challenge_id):
        team = request.user.team
        if team is None:
            return fail("USER_HAS_NO_TEAM", "소속된 팀이 없습니다", 404)

        flag = request.data.get("flag")
        if not flag:
            return fail("INVALID_REQUEST", "요청 값이 올바르지 않습니다", 400)

        challenge = Challenge.objects.filter(challenge_id=challenge_id).first()
        if challenge is None:
            return fail("CHALLENGE_NOT_FOUND", "존재하지 않는 문제 ID입니다.", 404)

        opened_challenge = OpenedChallenge.objects.filter(team=team, challenge=challenge).first()
        if opened_challenge is None:
            return fail("CHALLENGE_LOCKED", "아직 개방되지 않은 문제입니다.", 403)

        if Solve.objects.filter(team=team, challenge=challenge).exists():
            return fail("ALREADY_SOLVED", "이미 정답을 맞춘 문제입니다.", 409)

        now = timezone.now()
        submitted_flag_hash = hash_flag(flag)

        with transaction.atomic():
            flag_lock, _ = FlagSubmissionLock.objects.select_for_update().get_or_create(
                team=team,
                challenge=challenge,
            )

            if flag_lock.locked_until and flag_lock.locked_until > now:
                retry_after_seconds = int((flag_lock.locked_until - now).total_seconds())
                FlagSubmission.objects.create(
                    team=team,
                    user=request.user,
                    challenge=challenge,
                    submitted_flag_hash=submitted_flag_hash,
                    result=FlagSubmission.SubmissionResult.TOO_MANY_ATTEMPTS,
                )
                return fail(
                    "TOO_MANY_ATTEMPTS",
                    "잘못된 플래그를 3회 연속 제출했습니다. 30초 후 다시 시도해주세요.",
                    429,
                    {"retry_after_seconds": retry_after_seconds},
                )

            if flag_lock.locked_until and flag_lock.locked_until <= now:
                flag_lock.failed_count = 0
                flag_lock.locked_until = None

            if not is_correct_flag(flag, challenge.flag_hash):
                flag_lock.failed_count += 1
                flag_lock.last_failed_at = now

                if flag_lock.failed_count >= 3:
                    flag_lock.locked_until = now + timedelta(seconds=30)
                    result = FlagSubmission.SubmissionResult.INCORRECT
                    status_code = 429
                    code = "TOO_MANY_ATTEMPTS"
                    message = "잘못된 플래그를 3회 연속 제출했습니다. 30초 후 다시 시도해주세요."
                    data = {"retry_after_seconds": 30}
                else:
                    result = FlagSubmission.SubmissionResult.INCORRECT
                    status_code = 200
                    code = "INCORRECT_FLAG"
                    message = "틀린 플래그입니다."
                    data = None

                flag_lock.save(update_fields=["failed_count", "locked_until", "last_failed_at", "updated_at"])
                FlagSubmission.objects.create(
                    team=team,
                    user=request.user,
                    challenge=challenge,
                    submitted_flag_hash=submitted_flag_hash,
                    result=result,
                )
                return fail(code, message, status_code, data)

            is_extra_dice_granted = opened_challenge.solve_deadline_at >= now
            earned_score = challenge.score
            earned_mileage = 0

            try:
                solve = Solve.objects.create(
                    team=team,
                    challenge=challenge,
                    solved_by_user=request.user,
                    earned_score=earned_score,
                    earned_mileage=earned_mileage,
                    is_extra_dice_granted=is_extra_dice_granted,
                )
            except IntegrityError:
                return fail("ALREADY_SOLVED", "이미 정답을 맞춘 문제입니다.", 409)

            flag_lock.failed_count = 0
            flag_lock.locked_until = None
            flag_lock.last_failed_at = None
            flag_lock.save(update_fields=["failed_count", "locked_until", "last_failed_at", "updated_at"])

            FlagSubmission.objects.create(
                team=team,
                user=request.user,
                challenge=challenge,
                submitted_flag_hash=submitted_flag_hash,
                result=FlagSubmission.SubmissionResult.CORRECT,
            )

        return ok(
            message="정답입니다!",
            data={
                "challenge_id": str(challenge.challenge_id),
                "earned_score": number_value(earned_score),
                "earned_mileage": earned_mileage,
                "is_extra_dice_granted": is_extra_dice_granted,
                "solved_at": isoformat_z(solve.solved_at),
            },
        )
