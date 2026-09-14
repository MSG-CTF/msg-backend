# PR #72 검증

대상: 무인도 제거, 16·25번 룰렛, 찬스 카드 5종으로 정렬하는 백엔드 PR. main `1ea752a`의 시그니처 문제 기능을 통합해 로컬 검증했고, 이후 애플리케이션 변경 없는 `c54d1f4`의 배포 워크플로 갱신도 반영했다. 프론트엔드 변경은 포함하지 않는다.

## 검증 기준

PR #72에는 검증 시작 시점에 jongcoding의 직접 리뷰가 없었다. 아래 기존 리뷰에서 확인 가능한 요구 사항을 적용했다. 향후 별도 테스트나 새 요구 사항까지 통과했다고 의미하지 않는다.

| 기존 리뷰 기준 | 검증 위치와 확인 사항 |
| --- | --- |
| [#61: 주사위 충전 경계와 동시 갱신](https://github.com/MSG-CTF/msg-backend/pull/61#issuecomment-5559014937) | `apps/board/test_extra_roll_recharge.py`, `apps/board/test_dice_admin_concurrency.py`, `apps/adminpanel/tests.py`: 충전 직전·정각·직후, 3개 상한, 충전 시각 보존·재시작. 충전 조회·정답 제출·관리자 조정의 세 요청을 겹치고, 먼저 잠금을 얻는 요청을 각각 바꿔 보상 누락·중복 검사 |
| [#60: 문제 공개 상태와 실제 열기 API](https://github.com/MSG-CTF/msg-backend/pull/60#issuecomment-5558970591) | `apps/board/test_challenge_visibility.py`: 후보 재조회, 공개 전환과 문제 열기 경합, 이미 연 문제 접근 유지 |
| [#55: 마이그레이션 실패 후 최신 스키마 복원](https://github.com/MSG-CTF/msg-backend/pull/55#issuecomment-5556087437) | `apps/board/test_migrations.py`: downgrade 전 최신 leaf 저장·cleanup 등록, setup 및 assertion 실패 주입 후 복원, 실제 API로 이관된 게임 진행 |
| [#56: 동시 동일 키·정확한 재응답·원자성](https://github.com/MSG-CTF/msg-backend/pull/56#issuecomment-5553167911) | `apps/board/test_runtime_invariants.py`: 같은 키 두 요청 모두 200과 동일 JSON, 다른 키 중복 보상 409, 지급 후 또는 응답 저장 중 실패 시 잔액·기록·칸 소비·키 전체 롤백 |
| [#29: 문제 제출과 룰렛의 잠금 순서](https://github.com/MSG-CTF/msg-backend/pull/29#issuecomment-5552838763) | `apps/challenge/tests.py`: 실제 16·25번 각각에서 제출 우선·룰렛 우선 중첩을 강제하고 응답 두 개, 최종 점수, 30+50 마일리지와 기록을 검증 |
| [#29: 캐시 장애·프로세스 재시작](https://github.com/MSG-CTF/msg-backend/pull/29#issuecomment-5512233732) | `apps/board/test_runtime_invariants.py`, `apps/board/test_idempotency_restart.py`: cache.get/set 모두 실패하고 잔액이 바뀌어도 최초 JSON 재생, 별도 프로세스의 커밋 전후 종료 및 재시도 |

전체 테스트에는 동적 문제 점수의 재계산, 난이도별 30/60/120 마일리지, 팀 간 동시 정답 제출, KOTH 점수, 새 시그니처 점수와 랭킹·팀 조회의 합계 일치 검증도 포함된다.

## 재현 후 수정한 경로

- 카드 사용·폐기가 공용 `ChanceCard` 정의까지 잠가 경합 시 500을 반환했다. `select_for_update(of=("self",))`로 팀의 보유 카드 행만 잠그도록 수정하고, 다른 DB 연결이 카드 정의를 잠근 상태에서도 두 API가 성공하는지 검증한다.
- 보드 상태가 없는 새 팀이 주사위 확정을 먼저 호출하면 500을 반환했다. 상태 초기화 후 `409 NO_PENDING_ROLL`을 반환하도록 수정했다.
- 객체를 요구하는 API에 배열·문자열·숫자·boolean·null을 보내면 500을 반환했다. `400 INVALID_REQUEST`로 처리하고 게임 상태와 멱등성 기록이 바뀌지 않는지 검증한다. 본문 없는 API의 falsy JSON 우회도 함께 차단했다.

## 실행

Python 3.12, Django 5.2, 실제 PostgreSQL의 별도 테스트 DB를 사용한다. 동시성 테스트는 독립 DB 연결과 제한 시간을 사용하고, 작업자 예외를 테스트 결과로 전달한다. 캐시는 LocMemCache이며 캐시 장애는 명시적으로 주입한다. 외부 스케줄러·KOTH 서버 등은 각 기존 테스트의 대역을 사용한다.

```sh
python manage.py test apps --noinput --verbosity 2
python manage.py check
python manage.py makemigrations --check --dry-run
git diff --check
```

로컬 실행 결과: 전체 414개 통과(522.930초), 이후 추가한 세 요청 동시성 3개 통과(8.837초), 총 417개. 집중 검증 22개도 별도로 통과했다. Django check, 누락된 마이그레이션 검사, `git diff --check` 모두 정상이다. 새 커밋의 전체 GitHub CI 결과는 PR 본문의 검증 항목에 기록한다.

기존 게임의 업데이트는 `migrate` 후 백엔드 재시작으로 적용한다. `seed_board`는 진행을 초기화하므로 기존 게임의 업데이트에 사용하지 않는다.
