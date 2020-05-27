from unittest.mock import patch
from charmhelpers.core import unitdata
from reactive import containerd


def test_juju_proxy_changed():
    """Verify proxy changed bools are set as expected."""
    cached = {'http_proxy': 'foo', 'https_proxy': 'foo', 'no_proxy': 'foo'}
    new = {'http_proxy': 'bar', 'https_proxy': 'bar', 'no_proxy': 'bar'}

    # Test when nothing is cached
    db = unitdata.kv()
    db.get.return_value = None
    assert containerd._juju_proxy_changed() is True

    # Test when cache hasn't changed
    db.get.return_value = cached
    with patch('reactive.containerd.check_for_juju_https_proxy',
               return_value=cached):
        assert containerd._juju_proxy_changed() is False

    # Test when cache has changed
    db.get.return_value = cached
    with patch('reactive.containerd.check_for_juju_https_proxy',
               return_value=new):
        assert containerd._juju_proxy_changed() is True
