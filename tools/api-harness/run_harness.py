#!/usr/bin/env python
"""msgCTF API 명세 검증 하네스 실행 진입점.

사용 예:
  python run_harness.py verify-file fixtures --contract contract.yaml
  python run_harness.py verify-file specs/team_a.md specs/team_b.md
  python run_harness.py verify-notion --config config.yaml --token secret_xxx   # 레거시
"""
from harness.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
