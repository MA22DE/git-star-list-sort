"""Test package.

Credentials must come only from the environment a test sets, never from a
developer's ``.env`` or their ``~/.config`` file: otherwise a machine with real
credentials would silently exercise live paths and hide the hermetic behavior
these tests exist to pin.
"""

from __future__ import annotations

import os

os.environ["GIT_STAR_LIST_SORT_NO_DOTENV"] = "1"
