"""Compatibility entry point; implementation lives in playlist_extractor.reports."""
import sys
from playlist_extractor import reports as implementation

if __name__ == "__main__":
    sys.exit(implementation.main())

# Preserve imports used by existing scripts.
sys.modules[__name__] = implementation
