# MSG CTF Backend

MSG CTF의 참가자·팀·문제·점수·마일리지를 관리하는 Django API 서버입니다
문제 발행본을 가져와 배포 버전을 등록하고, 참가자의 실행 요청을 스케줄러로 연결합니다

[플랫폼 전체 소개](https://github.com/MSG-CTF/msg-ctf.github.io)

## 맡은 일

- 로그인과 팀 권한, 보드 이동·찬스카드·주사위 충전
- 일반 문제·시그니처 문제의 정답 제출, 동적 점수와 마일리지 지급
- GitHub 발행본 수집, 버전별 제공 파일·환경변수·비밀값 관리
- 인스턴스 생성·초기화·연장·종료와 응답 유실 후 작업 ID 복구
- KOTH 문제별 팀 토큰, 점수 수집, 관리자 미리보기
- 관리자 팀·문제·배포·QR 결제 처리와 감사 기록

## 함께 만든 사람들

| 기여자 | 기여 내용 | 관련 PR |
| --- | --- | --- |
| [gimgyumin0207](https://github.com/gimgyumin0207) | 문제·인스턴스 API, 참가자 파일, 릴리스 관리와 관리자 배포 기능 | [#8](https://github.com/MSG-CTF/msg-backend/pull/8) · [#80](https://github.com/MSG-CTF/msg-backend/pull/80) · [#191](https://github.com/MSG-CTF/msg-backend/pull/191) |
| [samdi70](https://github.com/samdi70) | 인증·팀 관리, 마이페이지, 마일리지·QR 결제와 릴리스 감사 기록 | [#5](https://github.com/MSG-CTF/msg-backend/pull/5) · [#18](https://github.com/MSG-CTF/msg-backend/pull/18) · [#136](https://github.com/MSG-CTF/msg-backend/pull/136) |
| [jiwonnii](https://github.com/jiwonnii) | 보드 규칙·보상, KOTH API·순위·문제별 팀 토큰 | [#14](https://github.com/MSG-CTF/msg-backend/pull/14) · [#20](https://github.com/MSG-CTF/msg-backend/pull/20) · [#204](https://github.com/MSG-CTF/msg-backend/pull/204) |
| [dnehdx](https://github.com/dnehdx) | 점수·리더보드 조회, 라인 독점, 조회 성능과 계정별 로그인 제한 | [#93](https://github.com/MSG-CTF/msg-backend/pull/93) · [#133](https://github.com/MSG-CTF/msg-backend/pull/133) · [#222](https://github.com/MSG-CTF/msg-backend/pull/222) |
| [goldsimchoi](https://github.com/goldsimchoi) | Gateway 접속 정보, 이미지 준비 후 전환, Windows 실행 연동 | [#148](https://github.com/MSG-CTF/msg-backend/pull/148) · [#178](https://github.com/MSG-CTF/msg-backend/pull/178) · [#212](https://github.com/MSG-CTF/msg-backend/pull/212) |
| [woalseee](https://github.com/woalseee) | 공통 CI 연결과 백엔드 VM 자동 배포 | [#25](https://github.com/MSG-CTF/msg-backend/pull/25) · [#69](https://github.com/MSG-CTF/msg-backend/pull/69) · [#99](https://github.com/MSG-CTF/msg-backend/pull/99) |
| [jongcoding](https://github.com/jongcoding) | 서비스 간 연동·운영 QA, 주사위 멱등성, 응답 유실·재시작 복구 | [#122](https://github.com/MSG-CTF/msg-backend/pull/122) · [#254](https://github.com/MSG-CTF/msg-backend/pull/254) · [#263](https://github.com/MSG-CTF/msg-backend/pull/263) |

2026-11-09까지 병합된 PR과 커밋을 기준으로 정리했습니다

## 기술 구성

Python · Django REST Framework · PostgreSQL · Redis · Docker

## 개발·운영 문서

- [문제 발행본 자동 수집](deploy/AUTOMATIC_PUBLICATIONS.md)
- [환경변수와 비밀값](docs/backend-execution-settings.md)
- [인스턴스 작업 복구](docs/instance-request-recovery.md)
- [배포와 운영 연결](deploy/platform_cd/README.md)
- [환경 설정 예시](.env.example)
- [로컬 Docker Compose 구성](docker-compose.yml)
