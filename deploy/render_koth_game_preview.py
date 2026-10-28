"""Write a candidate gateway config; installation and rollback remain explicit."""
import argparse
from pathlib import Path

from koth_preview_gateway import upgrade_game_preview, GAME_PROFILES


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--challenge-id', required=True)
    parser.add_argument('--origin', required=True)
    parser.add_argument('--upstream', required=True)
    parser.add_argument('--game', choices=sorted(GAME_PROFILES), required=True)
    args = parser.parse_args()
    rendered = upgrade_game_preview(args.config.read_text(encoding='utf-8'),
        args.challenge_id, args.origin, args.upstream, args.game)
    with args.output.open('x', encoding='utf-8', newline='\n') as output:
        output.write(rendered)
    print('Candidate written; validate it with Caddy before installation')


if __name__ == '__main__':
    main()
