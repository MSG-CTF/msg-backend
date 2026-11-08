<div align="center">
<h1>MSG CTF · Backend</h1>
<p>참가자·팀·문제·점수·마일리지를 관리하는 API 서버</p>
<p><a href="https://msg-ctf.github.io/platform.html">플랫폼</a> · <a href="#development">개발 문서</a></p>
</div>

<h2 align="center">Overview</h2>

<table align="center">
<tr><th width="260">대회 진행</th><th width="260">문제 운영</th><th width="260">관리·복구</th></tr>
<tr><td align="center">인증·팀·보드·보상</td><td align="center">발행본·실행 설정·KOTH</td><td align="center">배포·QR 결제·작업 복구</td></tr>
</table>

<h2 align="center">Contributors</h2>

<table align="center">
<tr><th align="center" colspan="12"><a href="https://github.com/MSG-CTF/msg-backend">Backend</a></th></tr>
<tr>
<td align="center" valign="top" colspan="3" width="195"><a href="https://github.com/gimgyumin0207"><img src="https://avatars.githubusercontent.com/u/244885819?v=4&amp;s=128" width="52" height="52" alt="gimgyumin0207"></a><br><a href="https://github.com/gimgyumin0207"><b>gimgyumin0207</b></a><br><sub>문제·인스턴스 API<br>파일·배포 버전</sub></td>
<td align="center" valign="top" colspan="3" width="195"><a href="https://github.com/samdi70"><img src="https://avatars.githubusercontent.com/u/46455764?v=4&amp;s=128" width="52" height="52" alt="samdi70"></a><br><a href="https://github.com/samdi70"><b>samdi70</b></a><br><sub>인증·팀·마일리지<br>QR 결제·감사 기록</sub></td>
<td align="center" valign="top" colspan="3" width="195"><a href="https://github.com/jiwonnii"><img src="https://avatars.githubusercontent.com/u/224767982?v=4&amp;s=128" width="52" height="52" alt="jiwonnii"></a><br><a href="https://github.com/jiwonnii"><b>jiwonnii</b></a><br><sub>보드·보상<br>KOTH 채점·팀 토큰</sub></td>
<td align="center" valign="top" colspan="3" width="195"><a href="https://github.com/dnehdx"><img src="https://avatars.githubusercontent.com/u/178199953?v=4&amp;s=128" width="52" height="52" alt="dnehdx"></a><br><a href="https://github.com/dnehdx"><b>dnehdx</b></a><br><sub>점수·순위<br>조회 성능·로그인 제한</sub></td>
</tr>
<tr><th align="center" colspan="4"><a href="https://github.com/MSG-CTF/secure-provisioner/tree/dev">Runtime</a></th><th align="center" colspan="4"><a href="https://github.com/MSG-CTF/jm_devsecops/tree/main">DevSecOps</a></th><th align="center" colspan="4"><a href="https://github.com/MSG-CTF">PM</a></th></tr>
<tr>
<td align="center" valign="top" colspan="4" width="260"><a href="https://github.com/goldsimchoi"><img src="https://avatars.githubusercontent.com/u/173701392?v=4&amp;s=128" width="52" height="52" alt="goldsimchoi"></a><br><a href="https://github.com/goldsimchoi"><b>goldsimchoi</b></a><br><sub>Gateway · 이미지 준비<br>Windows 실행 연결</sub></td>
<td align="center" valign="top" colspan="4" width="260"><a href="https://github.com/woalseee"><img src="https://avatars.githubusercontent.com/u/109592496?v=4&amp;s=128" width="52" height="52" alt="woalseee"></a><br><a href="https://github.com/woalseee"><b>woalseee</b></a><br><sub>공통 CI<br>VM 자동 배포</sub></td>
<td align="center" valign="top" colspan="4" width="260"><a href="https://github.com/jongcoding"><img src="https://avatars.githubusercontent.com/u/128033808?v=4&amp;s=128" width="52" height="52" alt="jongcoding"></a><br><a href="https://github.com/jongcoding"><b>jongcoding</b></a><br><sub>서비스 통합·운영 QA<br>중복 요청·장애 복구</sub></td>
</tr>
</table>

<details>
<summary>기여 기록 · 관련 PR</summary>

| 기여자 | 기여 내용 | 관련 PR |
| --- | --- | --- |
| [gimgyumin0207](https://github.com/gimgyumin0207) | 문제·인스턴스 API, 참가자 파일, 릴리스 관리와 관리자 배포 기능 | [#8](https://github.com/MSG-CTF/msg-backend/pull/8) · [#80](https://github.com/MSG-CTF/msg-backend/pull/80) · [#191](https://github.com/MSG-CTF/msg-backend/pull/191) |
| [samdi70](https://github.com/samdi70) | 인증·팀 관리, 마이페이지, 마일리지·QR 결제와 릴리스 감사 기록 | [#5](https://github.com/MSG-CTF/msg-backend/pull/5) · [#18](https://github.com/MSG-CTF/msg-backend/pull/18) · [#136](https://github.com/MSG-CTF/msg-backend/pull/136) |
| [jiwonnii](https://github.com/jiwonnii) | 보드 규칙·보상, KOTH API·순위·문제별 팀 토큰 | [#14](https://github.com/MSG-CTF/msg-backend/pull/14) · [#20](https://github.com/MSG-CTF/msg-backend/pull/20) · [#204](https://github.com/MSG-CTF/msg-backend/pull/204) |
| [dnehdx](https://github.com/dnehdx) | 점수·리더보드 조회, 라인 독점, 조회 성능과 계정별 로그인 제한 | [#93](https://github.com/MSG-CTF/msg-backend/pull/93) · [#133](https://github.com/MSG-CTF/msg-backend/pull/133) · [#222](https://github.com/MSG-CTF/msg-backend/pull/222) |
| [goldsimchoi](https://github.com/goldsimchoi) | Gateway 접속 정보, 이미지 준비 후 전환, Windows 실행 연동 | [#148](https://github.com/MSG-CTF/msg-backend/pull/148) · [#178](https://github.com/MSG-CTF/msg-backend/pull/178) · [#212](https://github.com/MSG-CTF/msg-backend/pull/212) |
| [woalseee](https://github.com/woalseee) | 공통 CI 연결과 백엔드 VM 자동 배포 | [#25](https://github.com/MSG-CTF/msg-backend/pull/25) · [#69](https://github.com/MSG-CTF/msg-backend/pull/69) · [#99](https://github.com/MSG-CTF/msg-backend/pull/99) |
| [jongcoding](https://github.com/jongcoding) | 서비스 간 연동·운영 QA, 주사위 멱등성, 응답 유실·재시작 복구 | [#122](https://github.com/MSG-CTF/msg-backend/pull/122) · [#254](https://github.com/MSG-CTF/msg-backend/pull/254) · [#263](https://github.com/MSG-CTF/msg-backend/pull/263) |

</details>

<h2 align="center">Tech Stack</h2>

<p align="center"><img src="https://raw.githubusercontent.com/MSG-CTF/msg-ctf.github.io/67e93a09ce630910f98ae56fb118ca1317af78e0/assets/tech/djangorest.svg" width="44" height="44" alt="Django REST" title="Django REST"> &nbsp; <img src="https://raw.githubusercontent.com/MSG-CTF/msg-ctf.github.io/67e93a09ce630910f98ae56fb118ca1317af78e0/assets/tech/postgresql.svg" width="44" height="44" alt="PostgreSQL" title="PostgreSQL"> &nbsp; <img src="https://raw.githubusercontent.com/MSG-CTF/msg-ctf.github.io/67e93a09ce630910f98ae56fb118ca1317af78e0/assets/tech/redis.svg" width="44" height="44" alt="Redis" title="Redis"> &nbsp; <img src="https://raw.githubusercontent.com/MSG-CTF/msg-ctf.github.io/67e93a09ce630910f98ae56fb118ca1317af78e0/assets/tech/docker.svg" width="44" height="44" alt="Docker" title="Docker"><br><sub>Django REST · PostgreSQL · Redis · Docker</sub><br><sub>Python · Django</sub></p>

## Development

- [문제 발행본 자동 수집](deploy/AUTOMATIC_PUBLICATIONS.md)
- [환경변수와 비밀값](docs/backend-execution-settings.md)
- [인스턴스 작업 복구](docs/instance-request-recovery.md)
- [배포와 운영 연결](deploy/platform_cd/README.md)
- [환경 설정 예시](.env.example)
- [로컬 Docker Compose 구성](docker-compose.yml)

<p align="center"><sub><a href="https://github.com/MSG-CTF/msg-ctf.github.io/blob/67e93a09ce630910f98ae56fb118ca1317af78e0/docs/tech-icons.md">아이콘 출처</a></sub></p>
