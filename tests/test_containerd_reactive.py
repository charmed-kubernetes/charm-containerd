import os
import json
from unittest.mock import patch
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
            "cert_file": "aGVsbG8gd29ybGQgY2VydC1maWxl"
        }]
        containerd.merge_custom_registries(dir, json.dumps(config))
        with open(os.path.join(dir, "my.other.registry.ca")) as f:
            assert f.read() == "hello world ca-file"
        with open(os.path.join(dir, "my.other.registry.key")) as f:
            assert f.read() == "hello world key-file"
        with open(os.path.join(dir, "my.other.registry.cert")) as f:
            assert f.read() == "hello world cert-file"
