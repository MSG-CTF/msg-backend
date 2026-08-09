
# Manual Commands

PR 전에 필요에 따라 아래 명령을 수동으로 실행한다.

## Repository hygiene

```bash
git diff --check
git status --short
```

## Migration check

```bash
python manage.py makemigrations --check --dry-run
```

## Secret/staged diff check

```bash
git diff --cached --name-only
git diff --cached
git ls-files
```

## CamelCase quick scan

`rg` 탐지는 보조수단이며, 최종 실패 판정은 JSON key/parser 기반 검사로 한다.

```bash
rg "\b[a-z]+[A-Z][a-zA-Z0-9_]*\b" apps config
```