# KOTH 관리자 게임 미리보기

공개 예정인 게임에 관리자 미리보기로 접속하면 게임 자체의 팀 토큰 로그인까지 이어집니다
출제자 소스와 게임 이미지는 수정하지 않습니다

관리자 권한과 미리보기 만료, 문제별 토큰, 팀 정지 여부를 함께 검사합니다
게임 세션도 이 권한에 묶습니다 다른 팀의 게임 인증정보를 섞거나, 토큰을 재발급하거나, 관리자를 삭제하면 이전 세션은 사용할 수 없습니다
Redis가 응답하지 않을 때는 인증을 허용하지 않고 503을 반환합니다

이 변경은 SCHEDULED 관리자 미리보기 경로에 적용됩니다
공개된 경기의 인증 방식, 경기 로직, 제출 파일, 점수 수집 경로는 기존 설정을 사용합니다
공개 경기의 네이티브 세션 회수까지 보장하는 변경은 아닙니다

## 적용 순서

1. 백엔드를 먼저 배포합니다 스키마 변경은 없습니다
2. 각 게임 VM의 현재 Caddyfile을 백업합니다 기존 v1 미리보기 설정이 있어야 합니다
3. 아래 도구로 후보 설정을 생성합니다 출력 파일은 덮어쓰지 않습니다
4. VM의 실제 Caddy 실행 파일로 후보 설정을 검증한 뒤 설치합니다
5. 관리자 미리보기에서 로그인과 상태 조회를 확인합니다 토큰이나 쿠키를 로그에 남기지 않습니다
6. 실패하면 백업한 Caddyfile을 복구하고 게이트웨이만 재시작합니다 게임 컨테이너는 재시작할 필요가 없습니다

```sh
python3 deploy/render_koth_game_preview.py \
  --config /etc/msgctf-koth/Caddyfile \
  --output /tmp/koth-preview-candidate.Caddyfile \
  --challenge-id CHALLENGE_UUID \
  --origin https://GAME_HOST \
  --upstream 127.0.0.1:GAME_PORT \
  --game koth-jenga

caddy validate --config /tmp/koth-preview-candidate.Caddyfile --adapter caddyfile
```

게임 식별자는 koth-card-battle-arena, koth-jenga, koth-swanopoly, koth-pixel-smuggler, koth-grand-tour-duel, koth-dependency-hell입니다
VM별 기존 upstream과 challenge ID를 사용해야 합니다
관리자 미리보기 쿠키는 게임 앱으로 전달하지 않습니다
로그인과 Card Battle API는 짧은 중계 요청을 사용하고, 나머지 게임 요청은 권한 확인 후 기존 upstream으로 바로 전달합니다
요청·응답은 각각 1MiB, 인증 중계의 응답 대기는 8초로 제한됩니다

## 검증

```sh
python manage.py test apps.koth
python -m unittest discover -s deploy -p 'test_koth_preview_gateway.py'
```

실제 게임 검증에서는 정상 로그인과 상태 조회를 먼저 성공시킨 뒤 다른 문제 토큰·기존 공용 토큰 차단을 확인합니다
이어 팀 정지, 토큰 무효화·재발급, 관리자 권한 제거, 미리보기 만료 후 기존 게임 세션이 차단되는지 확인합니다
