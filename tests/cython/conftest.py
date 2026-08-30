"""Load ``stario`` before Cython extensions.

Importing ``stario_cython.headers`` while ``stario.__init__`` is still running
re-enters the partial module (App → Writer → Headers). Completing the package
init first makes collection order-independent.
"""

import os

# Production sweeps with the Date header (1s). Tests use 50ms so header/idle
# cases finish in a few hundred milliseconds. Must be set before protocol import.
os.environ.setdefault("STARIO_CYTHON_TIMEOUT_SWEEP", "0.05")

import stario as _stario  # noqa: F401
