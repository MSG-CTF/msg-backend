# Jenga 미리보기 세션 갱신

출제자 최신 Jenga는 영구 세션을 사용해 응답마다 쿠키 서명 시각을 갱신합니다
로그인 때의 쿠키 문자열만 저장하면 다음 조회부터 정상 세션도 401로 막힙니다

플랫폼은 적용 완료된 실행 설정의 비밀값으로 서명과 12시간 유효기간을 검증합니다
검증된 팀·경기·세대·세션 표식을 미리보기 연결에 묶어, 같은 로그인 정보의 시각 갱신을 허용합니다
응답 순서가 바뀌어도 갱신 전후의 유효한 쿠키를 같은 로그인으로 처리합니다

위조 서명, 다른 팀·경기·세대, 만료된 쿠키는 거절합니다
문제별 토큰, 관리자 권한, 미리보기 유효기간과 세션 회수 검사는 요청마다 유지합니다
이전 문자열 비교 방식으로 만든 미리보기 세션은 다시 로그인해야 합니다

출제자 코드와 게임 이미지에는 변경이 없습니다
이전 Jenga의 session 쿠키와 다른 게임의 인증 방식은 유지합니다

[Flask의 영구 세션 갱신](https://flask.palletsprojects.com/en/stable/config/#SESSION_REFRESH_EACH_REQUEST)
[서명된 세션 쿠키](https://flask.palletsprojects.com/en/stable/api/#flask.sessions.SecureCookieSessionInterface)
