from django.core.management.base import BaseCommand

from apps.board.models import (
    Cell,
    ChanceCard,
    Challenge,
    DiceRoll,
    PendingDiceRoll,
    TeamBoardState,
    TeamCellCandidate,
    TeamCellConsumption,
    TeamChallengeAccess,
    TeamChanceCard,
)
from apps.accounts.models import User
from apps.board.services import get_default_team


BOARD_SIZE = 36
FIRST_CELL_INDEX = 1

SPECIAL_CELLS = {
    1: (Cell.CellType.START, "출발"),
    7: (Cell.CellType.CHANCE, "황금열쇠"),
    16: (Cell.CellType.QUARANTINE, "무인도"),
    21: (Cell.CellType.AIRPORT, "기차"),
    25: (Cell.CellType.ROULETTE, "룰렛"),
    30: (Cell.CellType.CHANCE, "황금열쇠"),
}

DIFFICULTY_COUNTS = {
    Cell.Difficulty.HARD: 6,
    Cell.Difficulty.MEDIUM: 12,
    Cell.Difficulty.EASY: 12,
}

# 강지원 확정 (2026-08-19): 칸별 난이도. cell_index -> Cell.Difficulty
CELL_DIFFICULTY = {
    2: Cell.Difficulty.HARD,
    3: Cell.Difficulty.MEDIUM,
    4: Cell.Difficulty.HARD,
    5: Cell.Difficulty.EASY,
    6: Cell.Difficulty.MEDIUM,
    8: Cell.Difficulty.MEDIUM,
    9: Cell.Difficulty.EASY,
    10: Cell.Difficulty.HARD,
    11: Cell.Difficulty.EASY,
    12: Cell.Difficulty.MEDIUM,
    13: Cell.Difficulty.MEDIUM,
    14: Cell.Difficulty.EASY,
    15: Cell.Difficulty.MEDIUM,
    17: Cell.Difficulty.EASY,
    18: Cell.Difficulty.MEDIUM,
    19: Cell.Difficulty.EASY,
    20: Cell.Difficulty.MEDIUM,
    22: Cell.Difficulty.MEDIUM,
    23: Cell.Difficulty.EASY,
    24: Cell.Difficulty.MEDIUM,
    26: Cell.Difficulty.HARD,
    27: Cell.Difficulty.EASY,
    28: Cell.Difficulty.EASY,
    29: Cell.Difficulty.HARD,
    31: Cell.Difficulty.MEDIUM,
    32: Cell.Difficulty.MEDIUM,
    33: Cell.Difficulty.EASY,
    34: Cell.Difficulty.EASY,
    35: Cell.Difficulty.HARD,
    36: Cell.Difficulty.EASY,
}

DIFFICULTY_LABELS = {
    Cell.Difficulty.HARD: "상",
    Cell.Difficulty.MEDIUM: "중",
    Cell.Difficulty.EASY: "하",
}

# 강지원 확정 (2026-08-19): 동아리 배정표 기준 분야 x 난이도별 출제 동아리.
# 슬래시(/)로 두 동아리가 적힌 칸은 문제 2개(동아리별 1개)를 뜻한다.
# 동아리명 철자는 강지원 확정: SeKurity, CodeCure
CATEGORY_CLUB_ASSIGNMENTS = {
    Cell.Difficulty.HARD: {
        "PWN": ["SWING"],
        "WEB": ["MJSEC"],
        "REV": ["Y-CERT"],
        "CRYPTO": ["SeKurity"],
        "WEB3": ["CodeCure"],
        "MISC": ["Aegis"],
    },
    Cell.Difficulty.MEDIUM: {
        "PWN": ["SeKurity", "Aegis"],
        "WEB": ["Aegis", "Y-CERT"],
        "REV": ["MJSEC"],
        "FORENSIC": ["CodeCure", "SWING"],
        "CRYPTO": ["SWING", "Y-CERT"],
        "MISC": ["SeKurity", "MJSEC"],
        "OSINT": ["CodeCure"],
    },
    Cell.Difficulty.EASY: {
        "PWN": ["Y-CERT", "CodeCure"],
        "WEB": ["CodeCure", "SeKurity"],
        "REV": ["SWING", "Aegis"],
        "FORENSIC": ["Y-CERT", "MJSEC"],
        "WEB3": ["SeKurity"],
        "MISC": ["SWING"],
        "OSINT": ["MJSEC", "Aegis"],
    },
}

CHANCE_CARDS = [
    {
        "card_id": "card_reroll",
        "name": "주사위 다시 굴리기",
        "description": "굴리기 전 위치 기준으로 새로 굴려서 즉시 재이동합니다.",
        "effect": "RE_ROLL",
        "usage_timing": ChanceCard.UsageTiming.POST_ROLL,
    },
    {
        "card_id": "card_roll_twice_choose",
        "name": "주사위 2회 굴림 후 선택",
        "description": "주사위를 굴리기 전에 두 번 굴린 뒤 한 값을 골라 이동합니다.",
        "effect": "ROLL_TWICE_CHOOSE",
        "usage_timing": ChanceCard.UsageTiming.PRE_ROLL,
    },
    {
        "card_id": "card_move_offset",
        "name": "주변 칸 이동",
        "description": "현재 위치에서 앞/뒤 1~3칸을 추가로 이동합니다.",
        "effect": "MOVE_OFFSET",
        "usage_timing": ChanceCard.UsageTiming.POST_ROLL,
    },
    {
        "card_id": "card_free_travel",
        "name": "세계여행",
        "description": "원하는 칸으로 자유롭게 이동합니다.",
        "effect": "FREE_MOVE",
        "usage_timing": ChanceCard.UsageTiming.PRE_ROLL,
    },
    {
        "card_id": "card_extra_roll",
        "name": "주사위 보너스",
        "description": "주사위를 굴릴 수 있는 횟수가 1회 늘어납니다.",
        "effect": "GRANT_EXTRA_ROLL",
        "usage_timing": ChanceCard.UsageTiming.PRE_ROLL,
    },
    {
        "card_id": "card_quarantine_defense",
        "name": "무인도 방어",
        "description": "탈출 시도 소모 없이 즉시 무인도에서 탈출합니다.",
        "effect": "QUARANTINE_ESCAPE_FREE",
        "usage_timing": ChanceCard.UsageTiming.QUARANTINE_STATE,
    },
    {
        "card_id": "card_move_to_quarantine",
        "name": "무인도 이동",
        "description": "즉시 무인도 칸으로 강제 이동합니다.",
        "effect": "FORCE_MOVE_TO_QUARANTINE",
        "usage_timing": ChanceCard.UsageTiming.PRE_ROLL,
    },
]


class Command(BaseCommand):
    help = "Seed the fixed board, 30 demo challenges, and the single default team state."

    def handle(self, *args, **options):
        PendingDiceRoll.objects.all().delete()
        DiceRoll.objects.all().delete()
        TeamChanceCard.objects.all().delete()
        TeamCellConsumption.objects.all().delete()
        TeamBoardState.objects.all().delete()
        TeamCellCandidate.objects.all().delete()
        TeamChallengeAccess.objects.all().delete()
        Challenge.objects.all().delete()
        Cell.objects.all().delete()
        ChanceCard.objects.all().delete()

        board_indexes = range(FIRST_CELL_INDEX, FIRST_CELL_INDEX + BOARD_SIZE)
        challenge_indexes = [i for i in board_indexes if i not in SPECIAL_CELLS]
        if set(challenge_indexes) != set(CELL_DIFFICULTY):
            self.stderr.write(
                f"challenge cells({len(challenge_indexes)}) != difficulty map({len(CELL_DIFFICULTY)})"
            )
            return

        for difficulty, count in DIFFICULTY_COUNTS.items():
            club_total = sum(len(clubs) for clubs in CATEGORY_CLUB_ASSIGNMENTS[difficulty].values())
            if club_total != count:
                self.stderr.write(
                    f"{difficulty} club assignment count({club_total}) != difficulty count({count})"
                )
                return

        cells = []
        for cell_index in board_indexes:
            if cell_index in SPECIAL_CELLS:
                cell_type, name = SPECIAL_CELLS[cell_index]
                cells.append(Cell(cell_index=cell_index, type=cell_type, name=name))
                continue

            difficulty = CELL_DIFFICULTY[cell_index]
            cells.append(
                Cell(
                    cell_index=cell_index,
                    type=Cell.CellType.CHALLENGE,
                    difficulty=difficulty,
                    name=f"문제({DIFFICULTY_LABELS[difficulty]})",
                )
            )

        Cell.objects.bulk_create(cells)

        challenges = []
        challenge_number = 1
        for difficulty in [Cell.Difficulty.EASY, Cell.Difficulty.MEDIUM, Cell.Difficulty.HARD]:
            label = DIFFICULTY_LABELS[difficulty]
            for category, clubs in CATEGORY_CLUB_ASSIGNMENTS[difficulty].items():
                for club_name in clubs:
                    challenges.append(
                        Challenge(
                            challenge_number=challenge_number,
                            title=f"{label} 문제 {challenge_number}",
                            category=category,
                            club_name=club_name,
                            difficulty=difficulty,
                            description=f"{label} 난이도 데모 문제입니다.",
                            flag=f"MSG{{challenge_{challenge_number:02d}}}",
                            score={
                                Cell.Difficulty.EASY: 100,
                                Cell.Difficulty.MEDIUM: 200,
                                Cell.Difficulty.HARD: 300,
                            }[difficulty],
                        )
                    )
                    challenge_number += 1

        Challenge.objects.bulk_create(challenges)

        ChanceCard.objects.bulk_create([ChanceCard(**card) for card in CHANCE_CARDS])

        team = get_default_team()
        TeamBoardState.objects.update_or_create(
            team=team,
            defaults={
                "position_id": 1,
                "dice_rolls_left": 1,
                "active_challenge_access": None,
            },
        )

        if not User.objects.filter(login_id="demo_leader").exists():
            leader = User(login_id="demo_leader", nickname="데모팀장", team=team, is_leader=True)
            leader.set_password("demo1234")
            leader.save()
        if not User.objects.filter(login_id="demo_member").exists():
            member = User(login_id="demo_member", nickname="데모팀원", team=team, is_leader=False)
            member.set_password("demo1234")
            member.save()

        self.stdout.write(
            f"보드 칸 36개, 문제 {len(challenges)}개, 기본 팀 '{team.team_name}' 준비 완료 "
            f"(문제 칸 {len(challenge_indexes)}개, 로그인: demo_leader/demo_member, 비밀번호 demo1234)"
        )
