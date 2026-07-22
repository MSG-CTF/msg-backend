from django.utils import timezone

DICE_TIME_LIMIT = 900 

#15분=900초
#이 시간 안에 풀면 점수, 마일리지, 주사위 받음

def elapsed_seconds(startTime): #경과시간
    return (timezone.now()-startTime).total_seconds()

def problem_remaining_seconds(startTime):
    remaining = DICE_TIME_LIMIT - elapsed_seconds(startTime)
    return max(0, int(remaining)) #15분 문제 타이머는 0 밑으로 안내려감

def is_bonus(startTime): #15분 안에 풀면 주사위 또
    return elapsed_seconds(startTime) <= DICE_TIME_LIMIT

def problem_remaining_display(startTime):
    total = problem_remaining_seconds(startTime)
    minutes = total // 60
    seconds = total % 60
    return f"{minutes:02d}:{seconds:02d}"