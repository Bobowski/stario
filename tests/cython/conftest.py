"""Load ``stario`` before Cython extensions.

Importing ``stario_cython.exchange`` while ``stario.__init__`` is still running
can re-enter the package. Completing the package init first makes collection
order-independent.
"""

import os

# Production sweeps with the Date header (1s). Tests use 50ms so header/idle
# cases finish in a few hundred milliseconds. Must be set before protocol import.
os.environ.setdefault("STARIO_CYTHON_TIMEOUT_SWEEP", "0.05")

import stario as _stario  # noqa: F401
