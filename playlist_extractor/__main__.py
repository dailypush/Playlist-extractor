"""Run `python -m playlist_extractor scan ...` or `... report ...`."""
import argparse
import sys

from . import batch, reports, scanner, terminal


def main(argv=None):
    parser = argparse.ArgumentParser(description='Identify songs in local DJ recordings and build reports.')
    parser.add_argument('command', choices=['scan', 'batch', 'report', 'ui'])
    argv = sys.argv[1:] if argv is None else argv
    args = parser.parse_args(argv[:1])
    return {'scan': scanner.main, 'batch': batch.main, 'report': reports.main, 'ui': terminal.main}[args.command](argv[1:])


if __name__ == '__main__':
    raise SystemExit(main())
