# 관리자 KOTH 게임 테스트

공개 예정 문제의 관리자 미리보기에서 게임 테스트 열기를 누르면 실제 게임을 5분 동안 열 수 있습니다
문제 상태와 공개 일정은 바뀌지 않으며, 게임 자체의 팀 토큰 로그인은 그대로 필요합니다

## 운영 테스트 토큰

팀이 없는 관리자가 미리보기의 팀 토큰을 누르면 그 관리자와 문제에 묶인 운영 테스트 토큰을 받습니다
GET /api/v1/koth/team_token 응답에 preview: true와 팀 이름 '운영 테스트'가 오고, 공개 예정(SCHEDULED) 문제에서만 발급합니다
진행 중이거나 끝난 문제는 409 KOTH_PREVIEW_TOKEN_UNAVAILABLE을 반환합니다

이 토큰은 그 관리자의 게임 테스트 로그인 순간에만 검증을 통과하고, 진행 중 문제나 일반 참가자 접속에서는 항상 거절됩니다
실제 팀을 만들지 않으므로 순위표·보드·마일리지에 들어가지 않습니다
게임 기록에 남은 운영 테스트 팀이 점수 응답에 섞여 와도 그 줄만 빼고 구간을 반영하며, 남은 팀끼리 부문별 순위를 다시 매깁니다
관리자 계정을 지워도 게임 기록을 계속 거를 수 있게 토큰 기록은 남깁니다

Backend의 KOTH_ADMIN_PREVIEW_ORIGINS에 준비된 게임의 HTTPS 원점만 쉼표로 구분해 등록합니다
기본값은 비어 있어 발급이 차단됩니다

관리자 API는 해당 관리자·문제·원점에 묶인 서명을 발급합니다
게임 게이트웨이는 매 요청마다 서명 만료와 현재 관리자 권한을 Backend에서 확인합니다
관리자 역할 삭제, 문제 상태·주소 변경, 허용 원점 제거 시 기존 서명도 거절됩니다

발급 URL의 접속 정보는 fragment에만 담습니다
게이트웨이 소유의 deploy/koth-admin-preview.html은 fragment를 즉시 지우고 POST로 교환합니다
응답은 Secure·HttpOnly·SameSite=Lax 호스트 쿠키를 설정합니다
게이트웨이는 이 쿠키를 게임 컨테이너로 전달하지 않아야 합니다
일반 참가자 접속과 채점 전용 호스트는 기존 시간·네트워크 제한을 유지합니다

서명 발급은 POST /api/v1/admin/koth/challenges/{challenge_id}/preview_sessions를 사용합니다
게이트웨이 확인은 /api/v1/koth/preview_sessions/authorize를 사용하며 일반 Access JWT는 받지 않습니다
게이트웨이가 고정한 X-KOTH-Preview-Challenge와 X-KOTH-Preview-Origin 헤더가 서명과 같아야 합니다
POST는 ticket 교환용이고 GET은 호스트 쿠키 확인용입니다

채점 API·공식 점수는 이 기능의 대상이 아닙니다
게임 서버 안에 남은 테스트 기록은 대회 전에 운영자가 정리해야 합니다
