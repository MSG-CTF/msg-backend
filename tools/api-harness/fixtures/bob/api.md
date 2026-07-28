## POST /login

이메일/비번으로 로그인함.

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
  "status": "ok",
  "result": {
    "userId": 1,
    "accessToken": "abc.def.ghi"
  },
  "message": "로그인 성공"
}
```

status: 200 OK, 400 Bad Request

## GET /api/users/{id}

유저 정보 조회.

### Response
```json
{
  "status": "ok",
  "result": {
    "userId": 1,
    "userName": "bob"
  },
  "message": null
}
```

status: 200 OK

## POST /api/messages

메시지 보내기.

### Request
```json
{
  "roomId": 10,
  "content": "hi"
}
```

### Response
```json
{
  "status": "ok",
  "result": {
    "messageId": 200,
    "sentAt": 1753700000
  },
  "message": null
}
```

status: 200 OK
