"""Read working checkpoints and self-contained playlist archives."""
import json
import os


def validate_state(state):
    if not isinstance(state, dict) or not isinstance(state.get('identity'), dict) or not isinstance(state.get('results'), dict):
        raise ValueError('Invalid saved recognition state; restore a backup before resuming')
    return state


def archive_state(document):
    if not isinstance(document, dict) or document.get('schema_version') != 2:
        raise ValueError('This playlist has no resumable scan metadata; its original checkpoint is required')
    return validate_state(document.get('scan_state'))


def load_state(folder):
    checkpoint = folder / 'checkpoint.json'
    if checkpoint.exists():
        return validate_state(json.loads(checkpoint.read_text(encoding='utf-8')))
    return archive_state(json.loads((folder / 'playlist.json').read_text(encoding='utf-8')))


def retire_checkpoint(state, folder):
    """Delete a completed working copy only after verifying its exported replacement."""
    checkpoint = folder / 'checkpoint.json'
    if state.get('sampling', {}).get('complete') is not True or not checkpoint.exists():
        return
    document = json.loads((folder / 'playlist.json').read_text(encoding='utf-8'))
    if archive_state(document) != state:
        raise ValueError('Exported scan metadata differs from the checkpoint; checkpoint retained')
    # Persist the archive's rename before removing the old recovery file.
    descriptor = os.open(folder, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    checkpoint.unlink()


def report_states(root):
    if root.is_file():
        document = json.loads(root.read_text(encoding='utf-8'))
        yield validate_state(document) if root.name == 'checkpoint.json' else archive_state(document)
        return
    folders = {p.parent for name in ('checkpoint.json', 'playlist.json') for p in root.rglob(name)}
    for folder in sorted(folders):
        if (folder / 'checkpoint.json').exists():
            yield load_state(folder)
        else:
            document = json.loads((folder / 'playlist.json').read_text(encoding='utf-8'))
            # Older presentation-only exports lack the original observations.
            if document.get('schema_version') == 2:
                yield archive_state(document)
