"""Compatibility entry point; implementation lives in playlist_extractor.cache."""
import sys
from playlist_extractor import cache as implementation

# Preserve imports used by existing scripts.
sys.modules[__name__] = implementation
