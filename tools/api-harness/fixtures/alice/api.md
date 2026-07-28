## POST /api/auth/login

로그인 API입니다.

### Request
```json
{
  "email": "test@example.com",
  "password": "1234"
}
```

### Response
```json
{
  "success": true,
  "data": {
    "user_id": 1,
    "access_token": "abc.def.ghi",
    "created_at": "2026-07-28T10:00:00Z"
  }
}
```

status: 201 Created, 401 Unauthorized

인증: 필요 없음 (로그인 전이므로)

---

## GET /api/users/:id

다음은 사용자 정보 조회 API 설명입니다:

- ✅ 로그인한 사용자만 호출 가능합니다
- ✅ Authorization: Bearer {token} 헤더가 필요합니다

이를 통해 정보노출 방지할 수 있습니다.

### Response
```json
{
  "success": true,
  "data": {
    "user_id": 1,
    "user_name": "alice",
    "user_email": "alice@example.com"
  }
}
```

status: 200 OK, 404 Not Found

궁금하신 점이 있으시면 언제든 말씀해주세요!

---

## POST /api/messages

메시지 전송 API

### Request
```json
{
  "room_id": 10,
  "content": "hello"
}
```

### Response
```json
{
  "success": true,
  "data": {
    "message_id": 100,
    "sent_at": "2026-07-28T10:05:00Z"
  }
}
```

status: 201 Created
