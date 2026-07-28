"""CLI 엔트리포인트 로직 (실행은 프로젝트 루트의 run_harness.py 참고)."""
from __future__ import annotations

import argparse
import os
import pathlib
import sys

import yaml

from .checks.ai_tone import scan_text
from .checks.conflicts import find_conflicts
from .checks.contract import run_all as run_contract_checks
from .checks.response_consistency import run_all as run_consistency_checks
from .models import SpecDocument
from .notion_source import (
    fetch_endpoint_page,
    fetch_page_markdown,
    load_offline_member,
    query_database_pages,
)
from .report import print_console_summary, render_markdown, to_json_dict, write_json
from .spec_parser import parse_endpoint_block, parse_spec_text


def _pages_from_endpoint_page_ids(team: str, page_ids: list[str], token: str) -> SpecDocument:
    raw_parts = []
    endpoints = []
    for page_id in page_ids:
        title, method, path, body = fetch_endpoint_page(page_id, token)
        raw_parts.append(body)
        if method and path:
            endpoints.append(parse_endpoint_block(team, title, method, path, body))
        else:
            # 속성에서 method/path를 못 찾으면 헤딩 기반 파싱으로 폴백
            sub_doc = parse_spec_text(team, title, body)
            endpoints.extend(sub_doc.endpoints)
    return SpecDocument(team=team, title=team, raw_text="\n\n".join(raw_parts), endpoints=endpoints)


def _build_member_doc_from_notion(member: dict, token: str) -> SpecDocument:
    team = member["name"]

    if "database_id" in member:
        # 노션 데이터베이스 안의 모든 페이지(=endpoint)를 자동으로 조회
        page_ids = query_database_pages(member["database_id"], token)
        return _pages_from_endpoint_page_ids(team, page_ids, token)

    if "page_ids" in member:
        # 페이지당 endpoint 1개 스타일, page_id를 수동으로 나열
        return _pages_from_endpoint_page_ids(team, member["page_ids"], token)

    if "page_id" in member:
        # 한 페이지 안에 'METHOD /path' 헤딩으로 여러 endpoint가 나열된 스타일
        title, md = fetch_page_markdown(member["page_id"], token)
        return parse_spec_text(team, title, md)

    sys.exit(f"[오류] members 항목 '{team}'에 database_id, page_id, page_ids 중 하나가 필요합니다.")


def build_specs(config: dict, token_override: str | None, offline_override: str | None):
    mode = config.get("mode", "offline")
    specs = []

    if mode == "notion":
        token_env = config.get("notion", {}).get("token_env", "NOTION_TOKEN")
        token = token_override or os.environ.get(token_env)
        if not token:
            sys.exit(f"[오류] Notion 토큰이 필요합니다. 환경변수 {token_env} 를 설정하거나 --token 을 지정하세요.")
        members = config.get("members", [])
        if not members:
            sys.exit("[오류] config의 'members' 목록이 비어 있습니다 (name, page_id 또는 page_ids 필요).")
        for m in members:
            specs.append(_build_member_doc_from_notion(m, token))

    elif mode == "offline":
        offline_dir = pathlib.Path(offline_override or config.get("offline_dir", "fixtures"))
        if not offline_dir.exists():
            sys.exit(f"[오류] 오프라인 디렉토리를 찾을 수 없습니다: {offline_dir}")
        for member_dir in sorted(p for p in offline_dir.iterdir() if p.is_dir()):
            text = load_offline_member(member_dir)
            if not text.strip():
                continue
            specs.append(parse_spec_text(member_dir.name, member_dir.name, text))

    else:
        sys.exit(f"[오류] 알 수 없는 mode: {mode} (notion | offline)")

    return specs


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="msgCTF 백엔드 팀 API 명세 검증 하네스: AI 말투 / 팀간 충돌 / 응답방식 일관성 검사",
    )
    parser.add_argument("--config", type=str, default=None, help="설정 yaml 경로 (config.example.yaml 참고)")
    parser.add_argument("--offline", type=str, default=None,
                         help="config 없이 바로 오프라인 디렉토리 지정 (팀원별 하위 폴더 구조)")
    parser.add_argument("--token", type=str, default=None, help="Notion integration token (미지정시 NOTION_TOKEN env 사용)")
    parser.add_argument("--out", type=str, default="reports/report.md", help="Markdown 리포트 출력 경로")
    parser.add_argument("--json-out", type=str, default=None, help="JSON 리포트 출력 경로 (선택)")
    parser.add_argument("--contract", type=str, default=None,
                         help="공통 규약 yaml 경로 (contract.yaml 참고) - 지정하면 규약 준수 검증을 추가로 수행")
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

    parser = build_arg_parser()
    args = parser.parse_args(argv)

    if args.config:
        with open(args.config, encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}
    elif args.offline:
        config = {"mode": "offline", "offline_dir": args.offline}
    else:
        parser.error("--config 또는 --offline 중 하나는 반드시 지정해야 합니다.")
        return 2

    specs = build_specs(config, args.token, args.offline)
    if not specs:
        print("[경고] 불러온 팀원 명세가 없습니다. config/offline 경로를 확인하세요.")
        return 1

    tone_reports = [scan_text(doc.team, doc.raw_text) for doc in specs]
    conflicts = find_conflicts(specs)
    consistency_issues = run_consistency_checks(specs)
    contract_violations = run_contract_checks(specs, args.contract) if args.contract else []

    print_console_summary(specs, tone_reports, conflicts, consistency_issues, contract_violations)

    out_path = pathlib.Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        render_markdown(specs, tone_reports, conflicts, consistency_issues, contract_violations),
        encoding="utf-8",
    )
    print(f"\n상세 Markdown 리포트: {out_path}")

    if args.json_out:
        write_json(args.json_out, to_json_dict(specs, tone_reports, conflicts, consistency_issues, contract_violations))
        print(f"JSON 리포트: {args.json_out}")

    has_findings = (
        any(r.score for r in tone_reports) or bool(conflicts) or bool(consistency_issues) or bool(contract_violations)
    )
    return 1 if has_findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
