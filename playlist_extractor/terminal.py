"""Interactive terminal workflow with a live, dependency-free scan display."""
import argparse
import csv
from pathlib import Path
import shutil
import sys
import threading
import time

from . import reports, scanner


def clean(value):
    return ''.join(c if c.isprintable() else ' ' for c in str(value))


class ProgressDisplay:
    def __init__(self, stream=None):
        self.stream = stream or sys.stdout
        self.interactive = self.stream.isatty()
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.latest = None
        self.started = time.monotonic()
        self.exports = []
        self.drawn = False

    def line(self):
        e = self.latest
        if not e:
            return 'Preparing recording... Ctrl+C to pause'
        total = e['total']
        fraction = e['done'] / total if total else 1
        bar = '#' * int(fraction * 16) + '-' * (16 - int(fraction * 16))
        elapsed = scanner.timestamp(time.monotonic() - self.started)
        offset = scanner.timestamp(e['offset']) if 'offset' in e else '--:--:--'
        return (f"{e['phase']} [{bar}] {e['done']}/{total} | {elapsed} elapsed | "
                f"{e['songs']} songs | {e['requests']} requests | {e['cache_hits']} cache | "
                f"{e['resumed']} resumed | {offset} {e['activity']}")

    def draw(self):
        width = shutil.get_terminal_size((100, 24)).columns
        self.clear()
        e = self.latest
        if e:
            total = e['total']
            filled = int(16 * e['done'] / total) if total else 16
            lines = [f"{e['phase']} [{'#' * filled}{'-' * (16 - filled)}] {e['done']}/{total}  Elapsed {scanner.timestamp(time.monotonic() - self.started)}",
                     f"Songs {e['songs']} | Requests {e['requests']} | Cache hits {e['cache_hits']} | Resumed {e['resumed']}",
                     f"{scanner.timestamp(e.get('offset', 0))}  {e['activity']} | Ctrl+C to pause"]
        else:
            lines = ['Preparing recording...', 'Saved results will be reused.', 'Ctrl+C to pause']
        self.stream.write('\n'.join(clean(line)[:max(1, width - 1)] for line in lines))
        self.drawn = True
        self.stream.flush()

    def clear(self):
        if self.drawn:
            self.stream.write('\r\033[2K\033[1A\r\033[2K\033[1A\r\033[2K')
            self.drawn = False

    def message(self, text):
        if self.interactive:
            self.clear()
        self.stream.write(clean(text) + '\n')
        if self.interactive:
            self.draw()
        self.stream.flush()

    def __call__(self, event):
        with self.lock:
            kind = event['kind']
            if kind == 'progress':
                self.latest = event
                if 'matches' in event:
                    label = '; '.join(f"{m['artist']} — {m['title']}" for m in event['matches']) or 'No match'
                    self.message(f"  {scanner.timestamp(event['offset'])}  {label}")
                if self.interactive:
                    self.draw()
                elif 'matches' in event or event['activity'] == 'Starting phase':
                    self.message(self.line())
            elif kind == 'message':
                self.message(event['text'])
            elif kind == 'finished':
                self.message('Sampling complete.' if event['complete'] else 'Paused at request limit; run again to resume.')
            elif kind == 'exported':
                self.exports.append(Path(event['folder']))

    def refresh(self):
        while not self.stop.wait(0.5):
            with self.lock:
                self.draw()

    def __enter__(self):
        self.thread = None
        if self.interactive:
            self.thread = threading.Thread(target=self.refresh, daemon=True)
            self.thread.start()
        return self

    def __exit__(self, *exc):
        self.stop.set()
        if self.thread:
            self.thread.join()
            self.clear()
            self.stream.flush()


def prompt(label, default):
    return input(f'{label} [{default}]: ').strip() or str(default)


def integer(label, default, minimum=1):
    while True:
        try:
            value = int(prompt(label, default))
            if value >= minimum:
                return value
        except ValueError:
            pass
        print(f'Enter a whole number of at least {minimum}.')


def choose_recording(source):
    files = sorted(p for p in source.rglob('*') if p.is_file() and p.suffix.lower() in scanner.MEDIA) if source.is_dir() else [source]
    files = [p for p in files if p.is_file()]
    if not files:
        print('No recordings found. Use Settings to change the source path.')
        return None
    print('\n0  All recordings in this source')
    for number, path in enumerate(files, 1):
        label = path.relative_to(source) if source.is_dir() else path.name
        print(f'{number}  {clean(label)}')
    choice = input('Recording number [1], or b to go back: ').strip() or '1'
    if choice.lower() == 'b':
        return None
    if choice.isdigit() and 0 <= int(choice) <= len(files):
        return source if int(choice) == 0 else files[int(choice) - 1]
    print('Invalid recording number.')
    return None


def show_playlist(output):
    files = sorted(output.glob('*/playlist.csv'))
    if not files:
        print('No playlists yet. Run a scan or rebuild exports first.')
        return
    print('\nAvailable playlists (includes saved snapshots):')
    for number, path in enumerate(files, 1):
        print(f'{number}  {clean(path.parent.name)}')
    choice = input('Playlist number [1], or b to go back: ').strip() or '1'
    if not choice.isdigit() or not 1 <= int(choice) <= len(files):
        return
    path = files[int(choice) - 1]
    with path.open(encoding='utf-8', newline='') as handle:
        rows = list(csv.DictReader(handle))
    print(f'\n{len(rows)} candidate songs | {clean(path)}\n')
    for number, row in enumerate(rows, 1):
        print(clean(f"{number:3}  {row['first_detected']}  {row['artist']} — {row['title']} [{row['status']}]"))
        if number % 20 == 0 and number < len(rows):
            if input('Enter for more, b to return: ').strip().lower() == 'b':
                break
    print('Detection times are approximate. Review single matches and version conflicts.')


def settings(args):
    args.source = Path(prompt('Recording file or folder', args.source)).expanduser()
    args.output = Path(prompt('Results folder', args.output)).expanduser()
    args.interval = integer('Seconds between samples (12-second clips)', args.interval, 12)
    args.delay = integer('Pause between provider requests, seconds', args.delay, 1)
    args.limit = integer('New requests per recording (0 = no cap)', args.limit, 0)
    args.refine = prompt('Extra checks around changes/gaps? y/n', 'y' if args.refine else 'n').lower() != 'n'


def scan_arguments(args, source, action):
    command = [str(source), '--output', str(args.output), '--provider', args.provider,
               '--interval', str(args.interval), '--delay', str(args.delay)]
    if args.limit:
        command += ['--max-requests', str(args.limit)]
    if not args.refine:
        command.append('--no-refine')
    mode = {'2': '--dry-run', '4': '--export-only', '5': '--seed-cache'}.get(action)
    if mode:
        command.append(mode)
    return command


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=Path('twitch-pyka'))
    parser.add_argument('--output', type=Path, default=Path('scan_results'))
    parser.add_argument('--provider', choices=['shazam'], default='shazam', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    args.interval, args.delay, args.limit, args.refine = 45, 3, 0, True
    if not sys.stdin.isatty():
        print('Interactive mode needs a terminal. Use docker run -it, or the scan/report commands.', file=sys.stderr)
        return 2
    while True:
        try:
            print('\n' + '=' * 64 + '\n  TWITCH PLAYLIST EXTRACTOR\n' + '=' * 64)
            print(f'Source: {clean(args.source)}\nResults: {clean(args.output)}')
            print(f'Provider: {args.provider} | sample every {args.interval}s | request pause {args.delay}s')
            print(f'Extra checks: {"on" if args.refine else "off"} | request cap/file: {args.limit or "none"}')
            print('\n1  Scan / resume recording       2  Estimate work (offline)\n'
                  '3  Browse playlists             4  Rebuild playlist exports\n'
                  '5  Seed recognition cache       6  Generate session reports\n'
                  '7  Settings                     q  Quit')
            action = input('\nChoose an action: ').strip().lower()
            if action == 'q':
                return 0
            if action == '7':
                settings(args)
            elif action == '3':
                show_playlist(args.output)
            elif action == '6':
                count, songs = reports.build_report(args.output, args.output / 'reports')
                print(f'Report saved: {args.output / "reports"} | {count} recordings, {songs} candidate songs')
            elif action in ('1', '2', '4', '5'):
                source = choose_recording(args.source)
                if source is None:
                    continue
                print(f'\n{args.provider}: {clean(source)} | Ctrl+C pauses and keeps saved results.')
                if action == '1':
                    print('New samples contact the selected service. Checkpoints and exact cache hits are reused.')
                with ProgressDisplay() as display:
                    try:
                        code = scanner.main(scan_arguments(args, source, action), event_handler=display)
                    except SystemExit as error:
                        code = error.code
                if code == 130:
                    print('Paused. Select Scan / resume to continue.')
                elif code:
                    print('The operation stopped. Check the error above; saved results are retained.')
            else:
                print('Choose one of the listed actions.')
        except (EOFError, KeyboardInterrupt):
            print('\nLeaving terminal app. Saved results remain available.')
            return 0
        except (OSError, ValueError, KeyError) as error:
            print(f'Could not complete the action: {clean(error)}')
