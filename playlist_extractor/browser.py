"""Read-only playlist and SQLite cache browsers for the live dashboard."""
from contextlib import closing
import curses
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import textwrap
import time

from .catalog import playlist_rows
from .storage import validate_state


def clean(value):
    return ''.join(c if c.isprintable() else ' ' for c in str(value))


def recordings(output, query=''):
    folders = {p.parent for name in ('checkpoint.json', 'playlist.json') for p in output.glob('*/' + name)}
    return [p for p in sorted(folders) if query.casefold() in p.name.casefold()]


def read_playlist(folder, query=''):
    """Prefer the current checkpoint so the running recording is visible too."""
    checkpoint = folder / 'checkpoint.json'
    try:
        state = validate_state(json.loads(checkpoint.read_text()))
    except FileNotFoundError:
        doc = json.loads((folder / 'playlist.json').read_text())
        songs = doc['playlist']
        name = doc.get('source', {}).get('filename') or folder.name
        complete = doc.get('recognition', {}).get('sampling_complete', False)
    else:
        _, songs, _ = playlist_rows(state)
        name = Path(state['identity'].get('path', folder.name)).name
        complete = state.get('sampling', {}).get('complete', False)
    query = query.casefold()
    rows = [s for s in songs if query in f"{s.get('artist', '')} {s.get('title', '')}".casefold()]
    return name, complete, rows


def cache_page(path, query='', page=0, page_size=50):
    """Open an existing DB read-only and release its read lock after one page."""
    if not path.is_file():
        return dict(total=0, matched=0, filtered=0, rows=[])
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=0.2)) as db:
        db.execute('PRAGMA query_only=ON')
        deadline = time.monotonic() + 0.25
        db.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        total, matched = db.execute("SELECT COUNT(*), COALESCE(SUM(matches != '[]'), 0) FROM results").fetchone()
        # JSON-escaped query handles non-ASCII song/artist names stored by json.dumps.
        needle = json.dumps(query, ensure_ascii=True)[1:-1].replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
        where = "WHERE matches LIKE ? ESCAPE '\\'"
        parameters = ('%' + needle + '%',)
        filtered = db.execute('SELECT COUNT(*) FROM results ' + where, parameters).fetchone()[0]
        raw = db.execute('SELECT digest, matches, created FROM results ' + where + ' ORDER BY rowid DESC LIMIT ? OFFSET ?', parameters + (page_size, max(0, page) * page_size)).fetchall()
    rows = []
    for digest, matches, created in raw:
        try:
            matches = json.loads(matches)
            label = '; '.join(f"{m.get('artist', '?')} — {m.get('title', '?')}" for m in matches) or 'No match'
        except (ValueError, TypeError, AttributeError):
            label = 'Unreadable cached result'
        rows.append(dict(digest=digest, label=label, created=created))
    return dict(total=total, matched=matched, filtered=filtered, rows=rows)


def put(screen, y, text, attr=0):
    height, width = screen.getmaxyx()
    if 0 <= y < height:
        try:
            screen.addnstr(y, 0, clean(text), max(0, width - 1), attr)
        except curses.error:
            pass


def search(screen, value):
    while True:
        height, _ = screen.getmaxyx()
        screen.move(max(0, height - 1), 0)
        screen.clrtoeol()
        put(screen, height - 1, '/ ' + value + '  (Enter apply, Esc cancel)')
        screen.refresh()
        key = screen.get_wch()
        if key in ('\n', '\r'):
            return value
        if key == '\x1b':
            return None
        if key in (curses.KEY_BACKSPACE, '\x7f', '\b'):
            value = value[:-1]
        elif isinstance(key, str) and key.isprintable():
            value += key


def show_detail(screen, fields):
    scroll = 0
    while True:
        height, width = screen.getmaxyx()
        lines = []
        for key, value in fields:
            lines.extend(textwrap.wrap(clean(f'{key}: {value}'), max(1, width - 2)) or [''])
            lines.append('')
        screen.erase()
        put(screen, 0, 'SAVED RESULT DETAILS', curses.A_BOLD)
        for y, line in enumerate(lines[scroll:scroll + max(1, height - 3)], 1):
            put(screen, y, line)
        put(screen, height - 1, 'b/q/Enter back | arrows scroll', curses.A_REVERSE)
        screen.refresh()
        key = screen.getch()
        if key in (ord('b'), ord('q'), 27, 10, 13):
            return
        if key in (curses.KEY_DOWN, ord('j')):
            scroll = min(max(0, len(lines) - 1), scroll + 1)
        elif key in (curses.KEY_UP, ord('k')):
            scroll = max(0, scroll - 1)


def show(screen, output, database=False):
    """Nested browser; b/q returns to monitoring without touching the worker."""
    selected = 0
    page = 0
    folder = None
    query = ''
    loaded = 0
    rows, header, detail = [], '', ''
    result = {'filtered': 0}
    screen.timeout(1000)
    while True:
        height, width = screen.getmaxyx()
        visible = max(1, height - 8)
        if time.monotonic() - loaded >= 3:
            try:
                if database:
                    result = cache_page(output / 'recognition-cache.sqlite3', query, page)
                    rows = result['rows']
                    header = f"{result['total']} cached samples | {result['matched']} matched | {result['total'] - result['matched']} no match"
                    detail = f"{result['filtered']} results | page {page + 1} | newest stored first | shared cache, not song counts"
                elif folder:
                    name, complete, rows = read_playlist(folder, query)
                    header = name
                    detail = f"{'Complete' if complete else 'In progress / partial'} | {len(rows)} songs | timestamps are detections, not song boundaries"
                else:
                    rows = recordings(output, query)
                    header = f'{len(rows)} saved recordings (including live checkpoints)'
                    detail = 'Select a recording and press Enter to view its playlist.'
                loaded = time.monotonic()
            except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as error:
                rows, header, detail = [], 'Data temporarily unavailable', str(error)
        selected = min(selected, max(0, len(rows) - 1))
        screen.erase()
        put(screen, 0, 'RECOGNITION DATABASE' if database else 'PLAYLIST LIBRARY', curses.A_BOLD)
        put(screen, 1, header)
        put(screen, 2, detail)
        put(screen, 3, f'Search: {query or "all"}  |  / search   c clear')
        start = (selected // visible) * visible
        for n, row in enumerate(rows[start:start + visible], start):
            if database:
                date = datetime.fromtimestamp(row['created'], timezone.utc).strftime('%m-%d %H:%M')
                label = f"{date} UTC | {row['label']}"
            elif folder:
                label = f"{row.get('first_detected', '?')} | {row.get('artist', '?')} — {row.get('title', '?')} | {row.get('detections', '?')} detections [{row.get('status', '?')}]"
            else:
                label = row.name
            put(screen, 5 + n - start, ('> ' if n == selected else '  ') + label, curses.A_REVERSE if n == selected else 0)
        if not rows:
            put(screen, 5, 'No results yet.' if not query else 'No results match this search.')
        if rows:
            row = rows[selected]
            info = f"Audio digest: {row['digest']}" if database else (f"ISRC: {row.get('isrc') or '-'} | Last detected: {row.get('last_detected', '-')} | Versions: {row.get('versions_detected', [])}" if folder else str(row))
            put(screen, height - 2, info)
        footer = 'b/q back | arrows select | PgUp/PgDn scroll | / search | c clear'
        footer += ' | n/p page | Enter details' if database else ' | Enter open'
        put(screen, height - 1, footer, curses.A_REVERSE)
        screen.refresh()
        key = screen.getch()
        if key in (ord('q'), ord('b'), 27):
            if folder:
                folder, selected, query, loaded = None, 0, '', 0
            else:
                return
        elif key in (10, 13, curses.KEY_ENTER) and rows:
            if not database and not folder:
                folder, selected, query, loaded = rows[selected], 0, '', 0
            else:
                row = rows[selected]
                if database:
                    fields = [('Result', row['label']), ('Audio digest', row['digest']), ('Stored UTC', datetime.fromtimestamp(row['created'], timezone.utc).isoformat())]
                else:
                    fields = [('Artist', row.get('artist')), ('Title', row.get('title')), ('First / last detected', f"{row.get('first_detected')} / {row.get('last_detected')}"), ('Detections', row.get('detections')), ('Review status', row.get('status')), ('ISRC', row.get('isrc') or '-'), ('Versions', row.get('versions_detected')), ('Saved folder', folder)]
                show_detail(screen, fields)
        elif key in (curses.KEY_DOWN, ord('j')):
            selected = min(max(0, len(rows) - 1), selected + 1)
        elif key in (curses.KEY_UP, ord('k')):
            selected = max(0, selected - 1)
        elif key == curses.KEY_NPAGE:
            selected = min(max(0, len(rows) - 1), selected + visible)
        elif key == curses.KEY_PPAGE:
            selected = max(0, selected - visible)
        elif key == ord('/'):
            screen.timeout(-1)
            try:
                value = search(screen, query)
            finally:
                screen.timeout(1000)
            if value is not None:
                query, selected, page, loaded = value, 0, 0, 0
        elif key == ord('c'):
            query, selected, page, loaded = '', 0, 0, 0
        elif database and key == ord('n') and (page + 1) * 50 < result['filtered']:
            page, selected, loaded = page + 1, 0, 0
        elif database and key == ord('p'):
            page, selected, loaded = max(0, page - 1), 0, 0
