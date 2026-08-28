"""Load ``stario`` before Cython extensions.

Importing ``stario_cython.headers`` while ``stario.__init__`` is still running
re-enters the partial module (App → Writer → Headers). Completing the package
init first makes collection order-independent.
"""

import stario as _stario  # noqa: F401
