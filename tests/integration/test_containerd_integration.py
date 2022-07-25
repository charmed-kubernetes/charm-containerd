import logging
import shlex
from pathlib import Path
import pytest
import yaml
import toml

log = logging.getLogger(__name__)


@pytest.mark.abort_on_fail
async def test_build_and_deploy(ops_test):
    """Build and deploy Containerd in bundle."""
    log.info("Build Charm...")
    charm = await ops_test.build_charm(".")

    overlays = [
        ops_test.Bundle("kubernetes-core", channel="edge"),
        Path("tests/data/charm.yaml"),
    ]

    log.info("Build Bundle...")
    bundle, *overlays = await ops_test.async_render_bundles(*overlays, charm=charm)

    log.info("Deploy Bundle...")
    model = ops_test.model_full_name
    cmd = f"juju deploy -m {model} {bundle} "
    cmd += " ".join(f"--overlay={f}" for f in overlays)
    rc, stdout, stderr = await ops_test.run(*shlex.split(cmd))
    assert rc == 0, f"Bundle deploy failed: {(stderr or stdout).strip()}"

    apps = [app for fragment in (bundle, *overlays) for app in yaml.safe_load(fragment.open())["applications"]]
    await ops_test.model.wait_for_idle(apps=apps, wait_for_active=True, timeout=60 * 60)


async def test_status_messages(ops_test):
    """Validate that the status messages are correct."""
    for unit in ops_test.model.applications["containerd"].units:
        assert unit.workload_status == "active"
        assert unit.workload_status_message == "Container runtime available"


async def test_upgrade_containerd_action(ops_test):
    """Test running upgrade-containerd action."""
    unit = ops_test.model.applications["containerd"].units[0]
    action = await unit.run_action("upgrade-containerd")
    output = await action.wait()  # wait for result
    assert output.data.get("status") == "completed"
    assert output.data.get("results", {}).get("runtime") == "containerd"


async def test_upgrade_containerd_dry_run_action(ops_test):
    """Test running upgrade-containerd action."""
    unit = ops_test.model.applications["containerd"].units[0]
    action = await unit.run_action("upgrade-containerd", **{"dry-run": True})
    output = await action.wait()  # wait for result
    assert output.data.get("status") == "completed"
    results = output.data.get("results", {})
    log.info(f"Upgrade dry-run results = '{results}'")
    assert results["available"] == results["installed"]
    assert results["can-upgrade"] == "False"


@pytest.fixture(scope="module")
async def juju_config(ops_test):
    """Apply configuration for a test, then revert after the test is completed."""

    async def setup(application, **new_config):
        to_revert[application] = (
            await ops_test.model.applications[application].get_config(),
            new_config,
        )
        await ops_test.model.applications[application].set_config(new_config)
        await ops_test.model.wait_for_idle(apps=[application], wait_for_active=True)

    to_revert = {}
    yield setup
    for app, (pre_test, settable) in to_revert.items():
        revert_config = {key: pre_test[key]["value"] for key in settable}
        await ops_test.model.applications[app].set_config(revert_config)
    await ops_test.model.wait_for_idle(apps=list(to_revert.keys()), wait_for_active=True)


@pytest.fixture(scope="module", params=["v1", "v2"])
async def config_version(request, juju_config):
    """Set the containerd config_version based on a parameter."""
    await juju_config("containerd", config_version=request.param)
    return request.param


async def containerd_config(unit):
    """Gather containerd config and load as a dict from its toml representation."""
    output = await unit.run("cat /etc/containerd/config.toml")
    stdout = output.data["results"].get("Stdout")
    assert stdout, "Containerd output was empty"
    return toml.loads(stdout)


async def containerd_hosts(unit):
    """Gather containerd hosts.toml and load each as a dict from its toml representation."""
    output = await unit.run("ls /etc/containerd/certs.d/*/hosts.toml")
    files = output.data["results"].get("Stdout")
    assert files, "Containerd output was empty"

    file_map = {}
    for fname in files.splitlines():
        output = await unit.run(f"cat {fname}")
        host = fname.split("/")[-2]
        file_map[host] = output.data["results"].get("Stdout")
    return {f: toml.loads(content) for f, content in file_map.items()}


async def test_containerd_registry_has_dockerio_mirror(config_version, ops_test):
    """Test gathering the list of registries."""
    plugin = "cri" if config_version == "v1" else "io.containerd.grpc.v1.cri"
    for unit in ops_test.model.applications["containerd"].units:
        config = await containerd_config(unit)
        registry = config["plugins"][plugin]["registry"]
        if config_version == "v1":
            assert "docker.io" in registry["mirrors"], "docker.io missing from containerd config"
            assert registry["mirrors"]["docker.io"]["endpoint"] == ["https://registry-1.docker.io"]
        else:
            assert registry["config_path"] == "/etc/containerd/certs.d"
            hosts = await containerd_hosts(unit)
            assert len(hosts) == 2, f"Wrong number of hosts.toml found in {hosts.keys()} on {unit.name}"
            assert "docker.io" in hosts


async def test_containerd_registry_with_private_registry(config_version, ops_test):
    """Test whether private registry config is represented in containerd."""
    registry_unit = ops_test.model.applications.get("docker-registry").units[0]
    plugin = "cri" if config_version == "v1" else "io.containerd.grpc.v1.cri"
    for unit in ops_test.model.applications["containerd"].units:
        hosts = await containerd_hosts(unit)
        hosts.pop("docker.io")
        hostname, host_toml = next(iter(hosts.items()))
        mirror = next(iter(host_toml["host"].values()))
        assert mirror["ca"], "TLS CA config isn't represented in the hosts.toml"
        assert mirror["client"], "TLS Client config isn't represented in the hosts.toml"

        if config_version == "v1":
            config = await containerd_config(unit)
            configs = config["plugins"][plugin]["registry"]["configs"]
            assert len(configs) == 1, "registry config isn't represented in config.toml"
            docker_registry = next(iter(configs))
            assert configs[docker_registry]["tls"], "TLS config isn't represented in the v1 config.toml"
            assert hostname in registry_unit.workload_status_message
