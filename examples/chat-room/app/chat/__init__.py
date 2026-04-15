"""
Chat feature slice for the ``chat-room`` example.

Common **feature package** layout (repeat for ``app.profile``, etc.):

- ``router.py`` — build and return a ``Router`` for this area; ``main`` mounts it.
- ``handlers.py`` — HTTP entrypoints (``Context``, ``Writer``) and factories that
  close over deps (``db``, ``relay``, …).
- ``views.py`` — HTML trees; pure functions of data, no I/O.
- ``models.py`` — domain types (and tiny demo helpers like random display names).
- ``relay_topics.py`` — dotted relay subject strings for this feature.
- ``db.py`` — persistence for this feature (SQLite adapter here).

We keep these split so each file has one job. Merging ``router`` into ``handlers``
is fine in tiny apps; combining ``models`` + ``db`` tends to hurt once SQL grows.

Static files for the whole app live under ``app/static/`` and mount in ``main``,
not inside this package — see ``main.py``.
"""
