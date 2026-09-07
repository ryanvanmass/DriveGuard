import os
import sys

# driveguard.py is a flat script at the repo root, not an installed package --
# make it importable as `import driveguard` regardless of how pytest's own
# rootdir/sys.path insertion behaves for this directory.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
