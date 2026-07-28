#!/usr/bin/env python
"""msgCTF API 명세 검증 하네스 실행 진입점.

사용 예:
  python run_harness.py --offline fixtures
  python run_harness.py --config config.yaml --token secret_xxx
"""
from harness.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
