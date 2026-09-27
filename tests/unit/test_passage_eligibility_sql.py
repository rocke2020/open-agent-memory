"""Source-window lookup retains passage policy without unrelated branches."""

from oam.db.eligibility import ELIGIBLE_PASSAGE_UNITS


def test_passage_lookup_keeps_active_source_and_generation_policy():
    assert "owner.state='active'" in ELIGIBLE_PASSAGE_UNITS
    assert "owner.index_generation_id=u.generation_id" in ELIGIBLE_PASSAGE_UNITS
    assert "p.visible_to IS NULL" in ELIGIBLE_PASSAGE_UNITS
    assert "s.active_version=e.source_version" in ELIGIBLE_PASSAGE_UNITS
    assert "e.learning_eligible" in ELIGIBLE_PASSAGE_UNITS
    assert "u.kind='fact'" not in ELIGIBLE_PASSAGE_UNITS
    assert "u.kind='insight'" not in ELIGIBLE_PASSAGE_UNITS
