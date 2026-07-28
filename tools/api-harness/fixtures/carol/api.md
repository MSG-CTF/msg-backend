## POST /api/auth/login

로그인.

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
    "access_token": "xyz",
    "created_at": "2026-07-28T10:00:00Z"
  }
}
```

status: 201 Created

## GET /api/users/{id}

유저 조회. Authorization 헤더 필요.

### Response
```json
{
  "success": true,
  "data": {
    "user_id": 1,
    "user_name": "carol"
  }
}
```

status: 200 OK

## POST /api/messages

### Request
```json
{
  "room_id": 10,
  "content": "yo"
}
```

### Response
```json
{
  "success": true,
  "data": {
    "message_id": 300,
    "sent_at": "2026-07-28T10:10:00Z"
  }
}
```

status: 201 Created
