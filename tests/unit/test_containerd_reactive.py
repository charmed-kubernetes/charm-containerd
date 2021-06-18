import os
import json
from unittest.mock import patch
from charmhelpers.core import unitdata
from charms.reactive import is_state
from reactive import containerd
import tempfile


def test_series_upgrade():
    """Verify series upgrade hook sets the status."""
    flags = {
        'upgrade.series.in-progress': True,
        'containerd.nvidia.invalid-option': False,
    }
    is_state.side_effect = lambda flag: flags[flag]
    assert containerd.status.blocked.call_count == 0
    with patch('reactive.containerd._check_containerd', return_value=False):
        containerd.charm_status()
    containerd.status.blocked.assert_called_once_with('Series upgrade in progress')


def test_merge_custom_registries():
    """Verify merges of registries."""
    with tempfile.TemporaryDirectory() as dir:
        config = [{
            "url": "my.registry:port",
            "username": "user",
            "password": "pass"
        }, {
            "url": "my.other.registry",
            "ca_file": "aGVsbG8gd29ybGQgY2EtZmlsZQ==",
            "key_file": "aGVsbG8gd29ybGQga2V5LWZpbGU=",
        }]
        ctxs = containerd.merge_custom_registries(dir, json.dumps(config), None)
        with open(os.path.join(dir, "my.other.registry.ca")) as f:
            assert f.read() == "hello world ca-file"
        with open(os.path.join(dir, "my.other.registry.key")) as f:
            assert f.read() == "hello world key-file"
        assert not os.path.exists(os.path.join(dir, "my.other.registry.cert"))

        for ctx in ctxs:
            assert 'url' in ctx

        # Remove 'my.other.registry' from config
        new_config = [{
            "url": "my.registry:port",
            "username": "user",
            "password": "pass"
        }]
        ctxs = containerd.merge_custom_registries(dir, json.dumps(new_config), json.dumps(config))
        assert not os.path.exists(os.path.join(dir, "my.other.registry.ca"))
        assert not os.path.exists(os.path.join(dir, "my.other.registry.key"))
        assert not os.path.exists(os.path.join(dir, "my.other.registry.cert"))


def test_juju_proxy_changed():
    """Verify proxy changed bools are set as expected."""
    cached = {'http_proxy': 'foo', 'https_proxy': 'foo', 'no_proxy': 'foo'}
    new = {'http_proxy': 'bar', 'https_proxy': 'bar', 'no_proxy': 'bar'}

    # Test when nothing is cached
    db = unitdata.kv()
    assert containerd._juju_proxy_changed() is True

    # Test when cache hasn't changed
    db.set('config-cache', cached)
    with patch('reactive.containerd.check_for_juju_https_proxy',
               return_value=cached):
        assert containerd._juju_proxy_changed() is False

    # Test when cache has changed
    with patch('reactive.containerd.check_for_juju_https_proxy',
               return_value=new):
        assert containerd._juju_proxy_changed() is True
