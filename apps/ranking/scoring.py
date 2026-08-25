"""
제오파디 다이나믹 스코어링

문제가 많이 풀릴수록 점수가 minimum_score까지 감소
새 정답 들어올때마다 해당 문제의 current_score 다시 계산
그 문제를 이미 푼 팀들도 바뀐 점수의 영향을 받는다 

initial_score	numeric	        시작 점수(최고점)
minimum_score	numeric	        최저 점수
decay	        int	            감소 기준
current_score	numeric	        현재 점수

"""



def calculate_dynamic_score(initial_score, minimum_score, decay, solved_team_count):
    if solved_team_count <= decay:
        return initial_score
    else:
        score = initial_score - (solved_team_count - decay)
        return max(score, minimum_score)


def recalculate_challenge_score(challenge_id):
    """
    한 문제에 새 정답이 들어왔을 때, 그 문제의 현재 점수와
    그 문제를 푼 팀들의 점수를 다시 계산한다.

    호출 시점: solves에 새 풀이가 저장된 직후.

    흐름:
      1. 이 문제를 푼 팀 수를 센다 (solves COUNT)
      2. calculate_dynamic_score로 새 current_score를 구한다
      3. challenges.current_score에 저장한다
      4. 이 문제를 푼 모든 팀의 team_score를 다시 계산한다
         (팀 점수 = 그 팀이 푼 문제들의 current_score 합계)
    """
     # 1. solved_team_count = Solve.objects.filter(challenge_id=...).count()

    # 2. new_score = calculate_dynamic_score(initial, minimum, decay, solved_team_count)

    # 3. challenge.current_score = new_score → save

    # 4. 이 문제 푼 팀들 각각 team_score 재계산
    