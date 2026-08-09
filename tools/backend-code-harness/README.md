# Backend Code Harness

이 하네스는 Django 백엔드 코드가 API contract를 지키는지 PR 전에 수동 점검하기 위한 기준 문서입니다.

## Relationship to api-harness

- `tools/api-harness/`: API 명세, sample response, contract 자체의 정합성 확인
- `tools/backend-code-harness/`: Django route, request 처리 key, response dict, serializer field, migration, secret, git 상태 확인

## Files

- `conventions.md`: 백엔드 코드 검증 규칙
- `commands.md`: PR 전 수동 실행 명령
- `canonical_fields.yaml`: 표준 필드명과 deprecated 필드명
- `forbidden_response_fields.json`: response에 노출되면 안 되는 필드