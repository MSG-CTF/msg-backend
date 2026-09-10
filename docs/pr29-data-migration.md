# PR 29 문제 개방 기록 이관

`challenge.0004_merge_opened_challenges_into_team_access`는 과거 개방 기록을
`TeamChallengeAccess`로 통합한 뒤 `OpenedChallenge` 테이블을 제거한다.
열린 문제 목록은 문제별 `BoardChallenge`의 동아리 정보를 읽으므로 이관 전에
보드 정보가 모두 존재해야 한다.

## 이관 전 확인

이전 스키마에서 다음 읽기 전용 조회로 누락된 문제 ID를 확인한다.
과거 개방 기록뿐 아니라 이미 있는 보드 개방 기록도 검사한다.

```sql
SELECT DISTINCT access.challenge_id
FROM (
    SELECT challenge_id FROM opened_challenges
    UNION
    SELECT challenge_id FROM team_challenge_accesses
) AS access
LEFT JOIN board_challenges AS meta ON meta.challenge_id = access.challenge_id
WHERE meta.id IS NULL
ORDER BY access.challenge_id;
```

누락이 있으면 마이그레이션도 변경을 시작하기 전에 `RuntimeError`로 중단하고
누락 개수와 문제 ID를 출력한다. 기존 보드 기록이 있는 문제도 생략하지 않는다.
이 단계에서 개방·완료 기록과 이전 테이블은 그대로 보존한다.

운영 문제 목록을 기준으로 해당 문제의 실제 `challenge_number`와 `club_name`을
확인해 `BoardChallenge`를 보완한다. 문제 번호는 다른 문제와 중복되지 않아야 한다.
임의 번호를 생성하거나 개방 기록을 삭제·건너뛰는 방식으로 통과시키지 않는다.
정보를 보완한 뒤 위 조회에서 누락이 없는 것을 확인하고 마이그레이션을 다시 실행한다.

## 이관 후 확인

- 기존 보드 개방 기록 유무와 풀이 완료 여부의 네 조합에서 개방 기록이 유지된다.
- 기존 보드 기록의 ID·개방 시각·출처 칸은 보존하고, 과거 정답 기록의 완료 상태와 시각을 반영한다.
- 해당 팀으로 `GET /api/v1/board/opened_challenges`를 조회해 개방 수·완료 수와 문제별 동아리·개방/완료 시각을 확인한다.

회귀 테스트: `apps.challenge.test_migrations.MergeOpenedChallengesMigrationTests`.
운영 DB의 실제 누락 여부는 배포 대상 DB에서 위 사전 조회로 확인한다.
