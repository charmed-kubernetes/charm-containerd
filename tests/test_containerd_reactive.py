from reactive import containerd


def test_series_upgrade():
    """Verify series upgrade hook sets the status."""
    assert containerd.status_set.call_count == 0
    containerd.pre_series_upgrade()
    assert containerd.status_set.call_count == 1
