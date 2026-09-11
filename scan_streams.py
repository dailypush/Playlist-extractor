"""Compatibility entry point; implementation lives in playlist_extractor.scanner."""
import sys
from playlist_extractor import scanner as implementation

if __name__ == "__main__":
    sys.exit(implementation.main())

# Preserve imports used by existing scripts.
sys.modules[__name__] = implementation
