import random

from django.core.management.base import BaseCommand

from apps.board.models import Cell, Challenge, DiceRoll, TeamBoardState, TeamCellCandidate, TeamChallengeAccess
from apps.teams.models import get_default_team


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

DIFFICULTY_LABELS = {
    Cell.Difficulty.HARD: "상",
    Cell.Difficulty.MEDIUM: "중",
    Cell.Difficulty.EASY: "하",
}

CATEGORIES = ["WEB", "SYSTEM", "CRYPTO", "REV", "MISC", "FORENSIC"]


class Command(BaseCommand):
    help = "Seed the fixed board, 30 demo challenges, and the single default team state."

    def handle(self, *args, **options):
        DiceRoll.objects.all().delete()
        TeamBoardState.objects.all().delete()
        TeamCellCandidate.objects.all().delete()
        TeamChallengeAccess.objects.all().delete()
        Challenge.objects.all().delete()
        Cell.objects.all().delete()

        pool = []
        for difficulty, count in DIFFICULTY_COUNTS.items():
            pool.extend([difficulty] * count)

        board_indexes = range(FIRST_CELL_INDEX, FIRST_CELL_INDEX + BOARD_SIZE)
        challenge_indexes = [i for i in board_indexes if i not in SPECIAL_CELLS]
        if len(challenge_indexes) != len(pool):
            self.stderr.write(
                f"challenge cells({len(challenge_indexes)}) != difficulty pool({len(pool)})"
            )
            return

        random.Random(0).shuffle(pool)

        cells = []
        for cell_index in board_indexes:
            if cell_index in SPECIAL_CELLS:
                cell_type, name = SPECIAL_CELLS[cell_index]
                cells.append(Cell(cell_index=cell_index, type=cell_type, name=name))
                continue

            difficulty = pool[challenge_indexes.index(cell_index)]
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
            for _ in range(DIFFICULTY_COUNTS[difficulty]):
                label = DIFFICULTY_LABELS[difficulty]
                challenges.append(
                    Challenge(
                        challenge_number=challenge_number,
                        title=f"{label} 문제 {challenge_number}",
                        category=CATEGORIES[(challenge_number - 1) % len(CATEGORIES)],
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

        team = get_default_team()
        TeamBoardState.objects.update_or_create(
            team=team,
            defaults={
                "position_id": 1,
                "dice_rolls_left": 1,
                "active_challenge_access": None,
            },
        )

        self.stdout.write(
            f"보드 칸 36개, 문제 {len(challenges)}개, 기본 팀 '{team.name}' 준비 완료 "
            f"(문제 칸 {len(challenge_indexes)}개)"
        )
