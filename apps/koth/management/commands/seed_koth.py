from django.core.management.base import BaseCommand

from apps.koth.models import KothChallenge, KothChallengeStatus, KothClub

# 동아리 6개 x 문제 2개 = 12문제 (koth-template admin.md "공개 API 연결" 확정, 2026-08-16).
CLUBS = [
    {
        "name": "동아리A",
        "challenges": [
            {"title": "koth_web_1", "category": "WEB", "open_group": 1},
            {"title": "koth_web_2", "category": "WEB", "open_group": 2},
        ],
    },
    {
        "name": "동아리B",
        "challenges": [
            {"title": "koth_pwn_1", "category": "PWN", "open_group": 1},
            {"title": "koth_pwn_2", "category": "PWN", "open_group": 2},
        ],
    },
    {
        "name": "동아리C",
        "challenges": [
            {"title": "koth_crypto_1", "category": "CRYPTO", "open_group": 1},
            {"title": "koth_crypto_2", "category": "CRYPTO", "open_group": 2},
        ],
    },
    {
        "name": "동아리D",
        "challenges": [
            {"title": "koth_rev_1", "category": "REV", "open_group": 1},
            {"title": "koth_rev_2", "category": "REV", "open_group": 2},
        ],
    },
    {
        "name": "동아리E",
        "challenges": [
            {"title": "koth_forensic_1", "category": "FORENSIC", "open_group": 1},
            {"title": "koth_forensic_2", "category": "FORENSIC", "open_group": 2},
        ],
    },
    {
        "name": "동아리F",
        "challenges": [
            {"title": "koth_misc_1", "category": "MISC", "open_group": 1},
            {"title": "koth_misc_2", "category": "MISC", "open_group": 2},
        ],
    },
]


class Command(BaseCommand):
    help = "Seed the 6 KOTH clubs and their 12 challenges (2 per club)."

    def handle(self, *args, **options):
        KothChallenge.objects.all().delete()
        KothClub.objects.all().delete()

        challenge_count = 0
        for club_entry in CLUBS:
            club = KothClub.objects.create(name=club_entry["name"])
            for challenge_entry in club_entry["challenges"]:
                KothChallenge.objects.create(
                    club=club,
                    title=challenge_entry["title"],
                    category=challenge_entry["category"],
                    open_group=challenge_entry["open_group"],
                    status=KothChallengeStatus.SCHEDULED,
                )
                challenge_count += 1

        self.stdout.write(f"KOTH 클럽 {len(CLUBS)}개, 문제 {challenge_count}개 준비 완료")
