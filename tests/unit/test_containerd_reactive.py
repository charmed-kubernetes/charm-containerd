import pathlib
import os
import json
from subprocess import CalledProcessError, STDOUT
import unittest.mock as mock
from urllib.error import HTTPError
import yaml

from charmhelpers.core import unitdata, host
from charmhelpers.core.templating import render
from charmhelpers.fetch import import_key
from charms.reactive import is_state, set_state
from reactive import containerd
import tempfile
import pytest

import jinja2


def test_series_upgrade():
    """Verify series upgrade hook sets the status."""
    flags = {
        "upgrade.series.in-progress": True,
        "containerd.nvidia.invalid-option": False,
    }
    is_state.side_effect = lambda flag: flags[flag]
    assert containerd.status.blocked.call_count == 0
    with mock.patch("reactive.containerd._check_containerd", return_value=False):
        containerd.charm_status()
    containerd.status.blocked.assert_called_once_with("Series upgrade in progress")


@mock.patch.object(containerd, "endpoint_from_flag")
@mock.patch.object(containerd, "ca_crt_path")
@mock.patch.object(containerd, "server_crt_path")
@mock.patch.object(containerd, "server_key_path")
def test_registry_relation(server_key_path, server_crt_path, ca_crt_path, endpoint_from_flag):
    """Verify writing to the registry db keyvalue store."""
    mock_registry = endpoint_from_flag.return_value
    mock_registry.registry_netloc = "http://registry.relation:5000"

    mock_registry.has_auth_basic.return_value = True
    mock_registry.basic_user = "user"
    mock_registry.basic_password = "password"

    mock_registry.has_tls.return_value = True

    ca_crt_path.__str__.return_value = "/path/to/ca"
    server_crt_path.__str__.return_value = "/path/to/crt"
    server_key_path.__str__.return_value = "/path/to/key"

    with mock.patch.object(containerd, "config_changed") as mock_config_changed:
        containerd.configure_registry()

    mock_config_changed.assert_called_once_with()
    set_registry_data = unitdata.kv().get("registry")
    assert set_registry_data == {
        "url": "http://registry.relation:5000",
        "host": "registry.relation:5000",
        "username": "user",
        "password": "password",
        "ca_file": None,
        "cert_file": None,
        "key_file": None,
        "insecure_skip_verify": None,
        "ca": "/path/to/ca",
        "cert": "/path/to/crt",
        "key": "/path/to/key",
    }


@pytest.mark.parametrize(
    "registry_errors",
    [
        ("", "Failed to decode json string"),
        ("{}", "custom_registries is not a list"),
        ("[1]", "registry #0 is not in object form"),
        ("[{}]", "registry #0 missing required field 'url'"),
        ('[{"url": 1}]', "registry #0 field url=1 is type int, not type str"),
        (
            '[{"url": "", "insecure_skip_verify": "FALSE"}]',
            "registry #0 field insecure_skip_verify=FALSE is type str, not type bool",
        ),
        (
            '[{"url": "", "why-am-i-here": "abc"}]',
            "registry #0 field why-am-i-here may not be specified",
        ),
        (
            '[{"url": "https://docker.io"}, {"url": "https://docker.io"}]',
            "registry #1 defines docker.io more than once",
        ),
        ("[]", None),
    ],
    ids=[
        "Invalid JSON",
        "Not a List",
        "List Item not an object",
        "Missing required field",
        "Non-stringly typed field",
        "Accidentally truthy",
        "Restricted field",
        "Duplicate host",
        "No errors",
    ],
)
def test_invalid_custom_registries(registry_errors):
    """Verify error status for invalid custom registries configurations."""
    registries, expected = registry_errors
    actual = containerd.invalid_custom_registries(registries)
    assert actual == expected


def test_registries_list():
    """Verify _registries_list resolves json to a list of objects, or returns default."""
    assert containerd._registries_list("[]") == []

    default = []
    assert containerd._registries_list("[{]", default) is default, "return default when invalid json"
    assert containerd._registries_list("{}", default) is default, "return default when valid json isn't a list"

    with pytest.raises(containerd.ValidationError) as ie:
        containerd._registries_list("[{]")
    assert "Failed to decode json string" in str(ie.value)

    with pytest.raises(containerd.ValidationError) as ie:
        containerd._registries_list("{}")
    assert "is not a list" in str(ie.value)


def test_merge_custom_registries(tmp_path):
    """Verify merges of registries."""
    config = [
        {"url": "my.registry:port", "username": "user", "password": "pass"},
        {
            "url": "my.other.registry",
            "ca_file": "aGVsbG8gd29ybGQgY2EtZmlsZQ==",
            "key_file": "aGVsbG8gd29ybGQga2V5LWZpbGU=",
            "cert_file": "abc",  # invalid base64 is ignored
        },
    ]
    ctxs = containerd.merge_custom_registries(tmp_path, json.dumps(config), None)
    with open(os.path.join(tmp_path, "my.other.registry.ca")) as f:
        assert f.read() == "hello world ca-file"
    with open(os.path.join(tmp_path, "my.other.registry.key")) as f:
        assert f.read() == "hello world key-file"
    assert not os.path.exists(os.path.join(tmp_path, "my.other.registry.cert"))

    for ctx in ctxs:
        assert ctx.url, "url must be assigned"

    # Remove 'my.other.registry' from config
    new_config = [{"url": "my.registry:port", "username": "user", "password": "pass"}]
    ctxs = containerd.merge_custom_registries(tmp_path, json.dumps(new_config), json.dumps(config))
    assert not os.path.exists(os.path.join(tmp_path, "my.other.registry.ca"))
    assert not os.path.exists(os.path.join(tmp_path, "my.other.registry.key"))
    assert not os.path.exists(os.path.join(tmp_path, "my.other.registry.cert"))


@pytest.mark.parametrize("version", ("v1", "v2"))
@pytest.mark.parametrize("gpu", ("off", "on"), ids=("gpu off", "gpu on"))
@mock.patch("reactive.containerd.endpoint_from_flag")
@mock.patch("reactive.containerd.config")
@mock.patch("charms.layer.containerd.can_mount_cgroup2", mock.Mock(return_value=False))
def test_custom_registries_render(mock_config, mock_endpoint_from_flag, gpu, version, tmp_path):
    """Verify exact rendering of config.toml files in both v1 and v2 formats."""

    class MockConfig(dict):
        def changed(self, *_args, **_kwargs):
            return False

    def jinja_render(source, target, context):
        env = jinja2.Environment(loader=jinja2.FileSystemLoader("src/templates"))
        template = env.get_template(source)
        with open(target, "w") as fp:
            fp.write(template.render(context))

    render.side_effect = jinja_render
    config = mock_config.return_value = MockConfig(config_version=version, gpu_driver="auto", runtime="auto")
    mock_endpoint_from_flag.return_value.get_sandbox_image.return_value = "sandbox-image"
    flags = {
        "containerd.nvidia.available": gpu == "on",
    }
    is_state.side_effect = lambda flag: flags[flag]
    config["custom_registries"] = json.dumps(
        [
            {"url": "my.registry:port", "username": "user", "password": {"interesting": "json"}},
            {"url": "my.other.registry", "insecure_skip_verify": True},
        ]
    )
    unitdata.kv().set(
        "registry",
        {
            "url": "http://db.registry:5000",
            "username": "user",
            "password": "pass",
            "ca": "/known/file/path/ca.crt",
            "cert": "/known/file/path/cert.crt",
            "key": "/known/file/path/cert.key",
        },
    )
    with mock.patch("reactive.containerd.CONFIG_DIRECTORY", tmp_path):
        containerd.config_changed()
    f_name = f"nvidia-{gpu}-{version}-config.toml"
    expected = pathlib.Path(__file__).parent / "test_custom_registries_render" / f_name
    target = pathlib.Path(tmp_path) / "config.toml"
    assert target.read_text() == expected.read_text()


def test_juju_proxy_changed():
    """Verify proxy changed bools are set as expected."""
    cached = {"http_proxy": "foo", "https_proxy": "foo", "no_proxy": "foo"}
    new = {"http_proxy": "bar", "https_proxy": "bar", "no_proxy": "bar"}

    # Test when nothing is cached
    db = unitdata.kv()
    assert containerd._juju_proxy_changed() is True

    # Test when cache hasn't changed
    db.set("config-cache", cached)
    with mock.patch("reactive.containerd.check_for_juju_https_proxy", return_value=cached):
        assert containerd._juju_proxy_changed() is False

    # Test when cache has changed
    with mock.patch("reactive.containerd.check_for_juju_https_proxy", return_value=new):
        assert containerd._juju_proxy_changed() is True


@pytest.fixture()
def default_config():
    """Mock out the config method from the charm default config."""
    config_yaml = yaml.safe_load(pathlib.Path("src/config.yaml").read_bytes())
    values = {key: obj.get("default") for key, obj in config_yaml["options"].items()}
    with mock.patch.object(containerd, "config", side_effect=values.get) as obj:
        yield obj


@mock.patch.object(containerd, "env_proxy_settings")
@mock.patch.object(containerd, "log")
@pytest.mark.usefixtures("default_config")
@pytest.mark.parametrize("success", [True, False])
def test_fetch_url_text(log, env_proxy_settings, success):
    """Test the fetch url method for success and failures."""

    def _responder(*_args):
        if success:
            return response
        raise HTTPError(the_url, 404, "Not Found", [], None)

    env_proxy_settings.return_value = None
    the_url = "https://google.com/robots.txt"
    response = mock.MagicMock(autospec="urllib.client.HTTPResponse")
    response.status = 200
    with mock.patch("urllib.request.OpenerDirector.open", side_effect=_responder) as mock_open:
        text = containerd.fetch_url_text([the_url])
    env_proxy_settings.assert_called_once_with()
    mock_open.assert_called_once_with(the_url)
    if success:
        assert text == [response.read.return_value.decode.return_value]
        response.read.assert_called_once_with()
        response.read.return_value.decode.assert_called_once_with()
        log.assert_not_called()
    else:
        assert text == [None]
        response.read.assert_not_called()
        log.assert_called_once_with(f"Cannot fetch url='{the_url}' with code 404 Not Found")


@mock.patch.object(containerd, "config_changed")
@mock.patch.object(containerd, "apt_autoremove")
@mock.patch.object(os, "remove")
@mock.patch.object(containerd, "apt_purge")
@mock.patch("builtins.open")
@pytest.mark.usefixtures("default_config")
def test_unconfigure_nvidia(mock_open, mock_apt_purge, mock_os_remove, mock_apt_autoremove, mock_config_changed):
    """Verify NVIDIA config is removed."""
    tmp_dir = tempfile.TemporaryDirectory()
    tmp_path = pathlib.Path(tmp_dir.name)
    sources_file = os.path.join(tmp_path, "nvidia.list")
    with mock.patch("reactive.containerd.NVIDIA_SOURCES_FILE", sources_file):
        containerd.unconfigure_nvidia()
    mock_apt_purge.assert_called_once
    mock_os_remove.assert_called_once
    mock_apt_autoremove.assert_called_once
    mock_config_changed.assert_called_once_with()
    assert not os.path.exists(sources_file)


@mock.patch.object(containerd, "fetch_url_text", return_value=["-key1-", "-key2-"])
@mock.patch("builtins.open")
@pytest.mark.usefixtures("default_config")
def test_configure_nvidia_sources(mock_open, fetch_url_text):
    """Verify NVIDIA apt sources are configured and keys are imported."""
    mock_lsb_release = dict(DISTRIB_ID="ubuntu", DISTRIB_RELEASE="20.04")
    import_key.reset_mock()
    with mock.patch.object(host, "lsb_release", return_value=mock_lsb_release):
        containerd.configure_nvidia_sources()

    # keys should be fetched from formatted urls
    fetch_url_text.assert_called_with(
        [
            "https://nvidia.github.io/nvidia-container-runtime/gpgkey",
            "https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2004/x86_64/3bf863cc.pub",
        ]
    )

    # import_key should be called twice with two key responses
    assert import_key.call_count == 2
    import_key.assert_has_calls(
        [
            mock.call("-key1-"),
            mock.call("-key2-"),
        ]
    )

    # sources file should be written out
    mock_open.assert_called_once_with("/etc/apt/sources.list.d/nvidia.list", "w")
    mock_file = mock_open.return_value.__enter__()
    mock_file.write.assert_called_once_with(
        "deb https://nvidia.github.io/libnvidia-container/stable/deb/$(ARCH) /\n"
        "deb https://nvidia.github.io/nvidia-container-runtime/ubuntu20.04/$(ARCH) /\n"
        "deb https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2004/x86_64 /"
    )


@mock.patch.object(containerd, "config_changed")
@mock.patch.object(containerd, "configure_nvidia_sources")
@mock.patch.object(containerd, "unconfigure_nvidia")
@mock.patch.object(containerd, "_test_gpu_reboot", mock.MagicMock())
@pytest.mark.usefixtures("default_config")
def test_install_nvidia_drivers(
    mock_unconfigure_nvidia,
    mock_configure_nvidia_sources,
    mock_config_changed,
):
    """Verify drivers are removed, config is done, and containerd config is updated."""
    set_state.reset_mock()
    containerd.install_nvidia_drivers()
    mock_unconfigure_nvidia.assert_called_once_with(reconfigure=False)
    mock_configure_nvidia_sources.assert_called_once_with()

    mock_config_changed.assert_called_once_with()
    set_state.assert_called_once_with("containerd.nvidia.ready")


@mock.patch.object(containerd, "application_version_set")
@mock.patch.object(containerd, "_check_containerd")
def test_containerd_version(mock_check, mock_version_set):
    """Verify containerd version parser."""
    version = b"""Client:
    Version:  1.5.9-0ubuntu1~20.04.4
      Revision:
      Go version: go1.13.8

    Server:
      Version:  1.5.9-0ubuntu1~20.04.4
      Revision:
      UUID: dc3fb3f1-3217-458b-8aaf-df2d7a4c7b91"""

    mock_check.return_value = version
    containerd.publish_version_to_juju()
    mock_version_set.assert_called_once_with("1.5.9")


@mock.patch.object(containerd, "set_state")
@mock.patch.object(containerd, "remove_state")
@mock.patch.object(containerd, "is_state")
@mock.patch.object(containerd, "check_output")
@pytest.mark.parametrize(
    "params",
    [
        (False, None),
        (True, None),
        (True, CalledProcessError(-1, "nvidia-smi", output=b"just a fatal error")),
        (True, FileNotFoundError),
    ],
    ids=[
        "nvidia not available",
        "nvidia-smi returns without exception",
        "nvidia-smi returns with CalledProcessError (non-reboot exception)",
        "nvidia-smi returns with FileNotFound",
    ],
)
def test_needs_gpu_reboot_false(check_output, is_state, remove_state, set_state, params):
    """Verify situations where no gpu induced reboot is needed."""
    nvidia_available, nvidia_smi_exception = params
    is_state.return_value = nvidia_available
    check_output.side_effect = nvidia_smi_exception

    assert not containerd._test_gpu_reboot()
    if not nvidia_available:
        check_output.assert_not_called()
    else:
        check_output.assert_called_once_with(["nvidia-smi"], stderr=STDOUT)
    set_state.assert_not_called()
    remove_state.assert_called_once_with("containerd.nvidia.needs_reboot")


@mock.patch.object(containerd, "set_state")
@mock.patch.object(containerd, "remove_state")
@mock.patch.object(containerd, "is_state")
@mock.patch.object(containerd, "check_output")
def test_needs_gpu_reboot_true(check_output, is_state, remove_state, set_state):
    """Verify situations where a gpu induced reboot is needed."""
    is_state.return_value = True
    check_output.side_effect = CalledProcessError(-1, "nvidia-smi", output=b"Driver/library version mismatch")
    assert containerd._test_gpu_reboot()
    check_output.assert_called_once_with(["nvidia-smi"], stderr=STDOUT)
    set_state.assert_called_once_with("containerd.nvidia.needs_reboot")
    remove_state.assert_not_called()
