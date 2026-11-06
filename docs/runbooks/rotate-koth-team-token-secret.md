# KOTH 서비스 키 교체 절차

[서비스 키 분리와 교체 절차](../service-key-rotation.md)를 정본으로 사용함

rotate_koth_team_token_secret 명령은 rotate_koth_team_token_key와 같은 감사형 명령의 호환 이름임
이전 yes-i-rotated-the-secret 플래그와 버전 없는 전체 삭제는 더 이상 지원하지 않음
수행자·사유·현재 버전·작업 UUID가 필요하며 같은 작업의 재시도는 새 토큰을 삭제하지 않음

키 원문은 비밀 저장소에만 보관하고 티켓과 감사 기록에는 비밀 버전 식별자와 검증 결과만 남김
폐기된 키로 되돌리지 않고 새로운 키로 복구하며 이전 키 작업자는 발급·검증을 거절함
참가자는 GET /api/v1/koth/team_token에 koth_challenge_id query를 지정해 새 토큰을 조회함
팀 토큰과 운영 테스트 토큰을 함께 무효화하고 테스트 팀 식별자는 채점 제외용으로 보존함

최초 등록, 작업자 전환, 이전 토큰·미리보기 세션 폐기, 실패 복구, 감사 조회와 OS 접근 감사는 정본의 절차를 따름
