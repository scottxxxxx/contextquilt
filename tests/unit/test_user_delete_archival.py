"""User deletes archive; they never hard-delete (boundary decision 2026-08-11).

The old DELETE /patches hard-deleted: no tombstone ever reached
`deleted[]` (computed from archived rows), other devices kept the patch
until the daily full sync, and for person patches the FK cascade
silently amputated every edge. These guards pin the replacement and the
one place hard deletion legitimately remains.
"""

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src"
MAIN = (SRC / "main.py").read_text()


def _delete_handler() -> str:
    m = re.search(
        r'@app\.delete\("/v1/quilt/\{user_id\}/patches/\{patch_id\}"\)?.*?'
        r'@app\.delete\("/v1/quilt/\{user_id\}"',
        MAIN, re.DOTALL,
    )
    assert m, "delete_patch handler not found"
    return m.group(0)


def test_user_delete_archives_with_cause():
    src = _delete_handler()
    assert "SET status = 'archived'" in src
    assert "'\"user_delete\"'" in src
    assert "updated_at = NOW()" in src, "the bump is what puts it in the next delta"


def test_user_delete_never_hard_deletes():
    """No DELETE FROM inside the handler: the row, its subject (the
    delta's join path), its edges, metrics and ACL all survive. The
    subject row especially: removing it would orphan the tombstone this
    change exists to serve."""
    assert "DELETE FROM" not in _delete_handler()


def test_second_delete_cannot_overwrite_the_cause():
    """Idempotent, and the WHERE guard keeps a re-delete (or a delete
    racing decay) from rewriting the original archive cause."""
    assert "AND COALESCE(status, 'active') = 'active'" in _delete_handler()


def test_the_contract_sentence_is_in_the_handler():
    """SS's condition, verbatim intent: excluded from recall and EVERY
    serving path, so deletion is real from the user's seat. The
    sentence lives in the docstring so the next reader inherits it."""
    src = _delete_handler()
    assert "EVERY serving" in src
    assert "real from the user's seat" in src


def test_account_purge_still_hard_deletes():
    """The one legitimate hard-delete: erasing a user's data, where
    leaving rows behind defeats the purpose."""
    m = re.search(
        r'@app\.delete\("/v1/quilt/\{user_id\}"\)?.*?async def delete_all_patches.*?return',
        MAIN, re.DOTALL,
    )
    assert m, "wipe endpoint not found"
    assert "DELETE FROM context_patches" in m.group(0)
