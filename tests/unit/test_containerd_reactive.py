import pathlib
import os
import json
from unittest.mock import patch

import pytest
from charmhelpers.core import unitdata
from charmhelpers.core.templating import render
from charms.reactive import is_state
from reactive import containerd
import tempfile

import jinja2


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


@pytest.mark.parametrize("version", ("v1", "v2"))
@patch('reactive.containerd.endpoint_from_flag')
@patch('reactive.containerd.config')
def test_custom_registries_render(mock_config, mock_endpoint_from_flag, version):
    """Verify exact rendering of config.toml files in both v1 and v2 formats."""
    class MockConfig(dict):
        def changed(self, *_args, **_kwargs):
            return False

    def jinja_render(source, target, context):
        env = jinja2.Environment(
            loader=jinja2.FileSystemLoader('templates')
        )
        template = env.get_template(source)
        with open(target, 'w') as fp:
            fp.write(template.render(context))

    render.side_effect = jinja_render
    config = mock_config.return_value = MockConfig(config_version=version)
    mock_endpoint_from_flag.return_value.get_sandbox_image.return_value = "sandbox-image"
    flags = {
        'containerd.nvidia.available': False,
    }
    is_state.side_effect = lambda flag: flags[flag]
    config['custom_registries'] = json.dumps([{
        "url": "my.registry:port",
        "username": "user",
        "password": "pass"
    }, {
        "url": "my.other.registry",
        "insecure_skip_verify": True
    }])

    with tempfile.TemporaryDirectory() as tmp_dir:
        with patch('reactive.containerd.CONFIG_DIRECTORY', tmp_dir):
            containerd.config_changed()
        expected = pathlib.Path(__file__).parent / "test_custom_registries_render" / (version + "_config.toml")
        target = pathlib.Path(tmp_dir) / "config.toml"
        assert list(target.open()) == list(expected.open())


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
