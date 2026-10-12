"""Load ``stario`` before Cython extensions.

Importing ``stario_cython.exchange`` while ``stario.__init__`` is still running
can re-enter the package. Completing the package init first makes collection
order-independent.
"""

import stario as _stario  # noqa: F401
