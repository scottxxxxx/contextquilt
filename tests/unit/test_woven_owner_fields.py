"""A woven tile says whose statement it is.

Scott opened his Twit project on 2026-09-07 and saw seven `preference`
tiles that were four TWiT hosts' opinions rendered as his: "Apple better
off without Johnny Ive" beside "Johnny Ive's original iMac design was
excellent", reading as one man contradicting himself.

`value.owner` was on every row the whole time and the tile dropped it.
ShoulderSurf was joining each tile back to /v1/quilt by patch_id to
recover it, which fails silently for any patch outside that route's cap.

These cover the pure half, the `owner` string on the tile. The resolved
half (`owner_entity_id`, `owned_by_self`) needs the entity graph and
lives in the route.
"""

from contextquilt.services.woven_digest import build_digest


def _patch(pid, ptype, text, owner=None, headline=None, origin="M1"):
    value = {"text": text, "headline": headline or text[:40]}
    if owner is not None:
        value["owner"] = owner
    return {"patch_id": pid, "patch_type": ptype, "value": value,
            "origin_id": origin, "created_at": "2026-09-07T00:00:00Z",
            "completed_at": None, "sensitivity": "normal",
            "last_observed_at": None}


def _tiles(patches, **kw):
    return build_digest(patches, limit=10, **kw)["patches"]


def test_the_owner_reaches_the_tile():
    """THE DEFECT. Nicholas said it, the tile said nothing."""
    tiles = _tiles([_patch("p1", "preference",
                           "Apple was better off without Johnny Ive",
                           owner="Nicholas")])
    assert len(tiles) == 1
    assert tiles[0]["owner"] == "Nicholas"


def test_an_ownerless_row_serves_null_not_an_empty_string():
    """Null is the state the ownerless-means-self rule reads. An empty
    string would be falsy in Python and truthy in Swift's decoder."""
    tiles = _tiles([_patch("p1", "preference", "Prefers the diff first")])
    assert tiles[0]["owner"] is None


def test_a_blank_owner_is_normalised_to_null():
    tiles = _tiles([_patch("p1", "preference", "Prefers the diff first",
                           owner="")])
    assert tiles[0]["owner"] is None


def test_every_tile_carries_the_key_even_when_nobody_owns_anything():
    """An additive field that is sometimes absent is worse than one that
    is always present and sometimes null: the client cannot tell "no
    owner" from "old server" (doc 19.3, an unstated field is an
    unemitted field)."""
    tiles = _tiles([
        _patch("p1", "decision", "Stopped Solana research"),
        _patch("p2", "takeaway", "Defer microphone mode"),
    ])
    assert len(tiles) == 2
    assert all("owner" in t for t in tiles)
    assert all(t["owner"] is None for t in tiles)


def test_the_owner_survives_ranking_and_pairs_with_its_own_tile():
    """Ordering is what makes a per-tile field dangerous: an owner that
    lands on the neighbouring tile is worse than no owner at all."""
    tiles = _tiles([
        _patch("p1", "preference", "Thinks the FTC is overreaching", owner="Leo"),
        _patch("p2", "preference", "Respects the original iMac design", owner="Iain"),
        _patch("p3", "preference", "Prefers the diff first"),
    ])
    by_id = {t["patch_id"]: t["owner"] for t in tiles}
    assert by_id["p1"] == "Leo"
    assert by_id["p2"] == "Iain"
    assert by_id["p3"] is None


def test_the_score_is_still_not_on_the_wire():
    """Guarding the neighbour: `score` shipped as `_salience` for one
    evening and GP caught it. Adding a field here must not reopen that."""
    tiles = _tiles([_patch("p1", "preference", "Prefers the diff first")])
    assert "score" not in tiles[0]
    assert "_salience" not in tiles[0]
