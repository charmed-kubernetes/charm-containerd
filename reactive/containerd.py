import contextlib
import os
import base64
import binascii
import json
import re
import traceback

from subprocess import check_call, check_output, CalledProcessError, STDOUT
from typing import List, Mapping, Optional, Set
import urllib.request
import urllib.error

from charms.reactive import (
    hook,
    when,
    when_not,
    set_state,
    is_state,
    remove_state,
    endpoint_from_flag,
    register_trigger,
    clear_flag,
)

from charms.layer import containerd, status
from charms.layer.container_runtime_common import (
    ca_crt_path,
    server_crt_path,
    server_key_path,
    check_for_juju_https_proxy,
)

from charmhelpers.core import host, unitdata

from charmhelpers.core.templating import render
from charmhelpers.core.hookenv import atexit, config, env_proxy_settings, log, application_version_set

from charmhelpers.core.kernel import modprobe

from charmhelpers.fetch import (
    apt_cache,
    apt_install,
    apt_update,
    apt_purge,
    apt_hold,
    apt_autoremove,
    apt_unhold,
    import_key,
)

from charmhelpers.fetch.ubuntu_apt_pkg import Package

NVIDIA_SOURCES_FILE = "/etc/apt/sources.list.d/nvidia.list"


def apt_packages(packages: Set[str]) -> Mapping[str, Package]:
    """Return a mapping of package names to Package classes.

    Ignores all packages which aren't available to apt
    Includes any package which wildcard matches with an installed package

    @param packages: List of packages for which to search
    @returns: Map of available packages
    """
    result = {}
    if not packages:
        return result

    cache = apt_cache()
    # also search for any already installed packages matching
    wildcards = [_ + "*" for _ in packages]
    packages = set(cache.dpkg_list(wildcards).keys()) | set(packages)

    for pkg_name in packages:
        try:
            result[pkg_name] = cache[pkg_name]
        except KeyError:
            log(f"Cannot find {pkg_name} in apt.")
    return result


@contextlib.contextmanager
def proxy_env():
    """Create a context to temporarily modify proxy in os.environ."""
    restore = {**os.environ}  # Copy the current os.environ
    # overwrite JUJU_CHARM_*_PROXY from config where available
    for key in ["http_proxy", "https_proxy", "no_proxy"]:
        val = config(key)
        if val:
            os.environ[f"JUJU_CHARM_{key.upper()}"] = val
    juju_proxies = env_proxy_settings() or {}
    os.environ.update(**juju_proxies)  # Insert or Update the os.environ
    yield os.environ
    for key in juju_proxies:
        del os.environ[key]  # remove any keys which were added or updated
    os.environ.update(**restore)  # restore any original values


def fetch_url_text(urls) -> List[Optional[str]]:
    """Fetch url text within a proxied environment.

    returns: None in the event the url yielded no response.
    """
    # updates os.environ to include juju proxy settings
    with proxy_env():
        handlers = [urllib.request.ProxyHandler()]
        opener = urllib.request.build_opener(*handlers)
        responses = []
        for url in urls:
            resp = None
            try:
                resp = opener.open(url).read().decode()
            except urllib.error.HTTPError as e:
                log(f"Cannot fetch url='{url}' with code {e.code} {e.reason}")
            except urllib.error.URLError as e:
                log(f"Cannot fetch url='{url}' with {e.reason}")
            finally:
                responses.append(resp)

    return responses


DB = unitdata.kv()

CONTAINERD_PACKAGE = "containerd"

register_trigger(when="config.changed.nvidia_apt_sources", clear_flag="containerd.nvidia.ready")
register_trigger(when="config.changed.nvidia_apt_packages", clear_flag="containerd.nvidia.ready")


def _check_containerd():
    """
    Check that containerd is running.

    `ctr version` calls both client and server side, so is a reasonable indication that everything's been set up
    correctly.

    :return: bytes
    """
    try:
        version = check_output(["ctr", "version"])
    except (FileNotFoundError, CalledProcessError):
        return None

    return version


def _juju_proxy_changed():
    """
    Check to see if the Juju model HTTP(S) proxy settings have changed.

    These aren't propagated to the charm so we'll need to do it here.

    :return: Boolean
    """
    cached = DB.get("config-cache", None)
    if not cached:
        return True  # First pass.

    new = check_for_juju_https_proxy(config)

    if (
        cached["http_proxy"] == new["http_proxy"]
        and cached["https_proxy"] == new["https_proxy"]
        and cached["no_proxy"] == new["no_proxy"]
    ):
        return False

    return True


def _test_gpu_reboot() -> bool:
    reboot = False
    if is_state("containerd.nvidia.available"):
        try:
            check_output(["nvidia-smi"], stderr=STDOUT)
        except CalledProcessError as cpe:
            log("Unable to communicate with the NVIDIA driver.")
            log(cpe)
            reboot = any(message in cpe.stdout.decode() for message in ["Driver/library version mismatch"])
        except FileNotFoundError as fne:
            log("NVIDIA SMI not installed.")
            log(fne)
    if reboot:
        set_state("containerd.nvidia.needs_reboot")
    else:
        remove_state("containerd.nvidia.needs_reboot")
    return reboot


@atexit
def charm_status():
    """
    Set the charm's status after each hook is run.

    :return: None
    """
    if is_state("upgrade.series.in-progress"):
        status.blocked("Series upgrade in progress")
    elif is_state("containerd.nvidia.invalid-option"):
        status.blocked("{} is an invalid option for gpu_driver".format(config().get("gpu_driver")))
    elif is_state("containerd.nvidia.fetch_keys_failed"):
        status.blocked("Failed to fetch nvidia_apt_key_urls.")
    elif is_state("containerd.nvidia.missing_package_list"):
        status.blocked("No NVIDIA packages selected to install.")
    elif is_state("containerd.nvidia.needs_reboot"):
        status.blocked("May need reboot to activate GPU.")
    elif _check_containerd():
        status.active("Container runtime available")
        set_state("containerd.ready")
    else:
        status.blocked("Container runtime not available")


def strip_url(url):
    """Strip the URL of protocol, slashes etc., and keep host:port.

    Examples:
        url: http://10.10.10.10:8000 --> return: 10.10.10.10:8000
        url: https://myregistry.io:8000/ --> return: myregistry.io:8000
        url: myregistry.io:8000 --> return: myregistry.io:8000
    """
    return url.rstrip("/").split(sep="://", maxsplit=1)[-1]


def update_custom_tls_config(config_directory, registries, old_registries):
    """
    Read registries config and remove old/write new tls files from/to disk.

    :param str config_directory: containerd config directory
    :param List registries: juju config for custom registries
    :param List old_registries: old juju config for custom registries
    :return: None
    """
    # Remove tls files of old registries; so not to leave uneeded, stale files.
    for registry in old_registries:
        for opt in ["ca", "key", "cert"]:
            file_b64 = registry.get("%s_file" % opt)
            if file_b64:
                registry[opt] = os.path.join(config_directory, "%s.%s" % (strip_url(registry["url"]), opt))
                if os.path.isfile(registry[opt]):
                    os.remove(registry[opt])

    # Write tls files of new registries.
    for registry in registries:
        for opt in ["ca", "key", "cert"]:
            file_b64 = registry.get("%s_file" % opt)
            if file_b64:
                try:
                    file_contents = base64.b64decode(file_b64)
                except (binascii.Error, TypeError):
                    log(traceback.format_exc())
                    log("{}:{} didn't look like base64 data... skipping".format(registry["url"], opt))
                    continue
                registry[opt] = os.path.join(config_directory, "%s.%s" % (strip_url(registry["url"]), opt))
                with open(registry[opt], "wb") as f:
                    f.write(file_contents)


def populate_host_for_custom_registries(custom_registries):
    """Populate host field from url if missing for custom registries.

    Examples:
        url: http://10.10.10.10:8000 --> host: 10.10.10.10:8000
        url: https://myregistry.io:8000/ --> host: myregistry.io:8000
        url: myregistry.io:8000 --> host: myregistry.io:8000
    """
    # only do minimal changes to custom_registries when conditions apply
    # otherwise return it directly as it is
    if isinstance(custom_registries, list):
        for registry in custom_registries:
            if not registry.get("host"):
                url = registry.get("url")
                if url:
                    registry["host"] = strip_url(url)

    return custom_registries


def insert_docker_io_to_custom_registries(custom_registries):
    """
    Ensure the default docker.io registry exists.

    Also gives a way for configuration to override the url for it.
    If a docker.io host entry doesn't exist, we'll add one.
    """
    if isinstance(custom_registries, list):
        if not any(d.get("host") == "docker.io" for d in custom_registries):
            custom_registries.insert(0, {"host": "docker.io", "url": "https://registry-1.docker.io"})
    return custom_registries


class InvalidCustomRegistriesError(Exception):
    """Error for Invalid Registry decoding."""


def _registries_list(registries, default=None):
    """
    Parse registry config and ensure it returns a list or raises ValueError.

    :param str registries: representation of registries
    :param default: if provided, return rather than raising exceptions
    :return: List of registry objects
    """
    registry_list = default
    try:
        registry_list = json.loads(registries)
    except json.JSONDecodeError:
        if default is None:
            raise

    if not isinstance(registry_list, list):
        if default is None:
            raise InvalidCustomRegistriesError("'{}' is not a list".format(registries))
        else:
            return default

    return registry_list


def merge_custom_registries(config_directory, custom_registries, old_custom_registries):
    """
    Merge custom registries and Docker registries from relation.

    :param str config_directory: containerd config directory
    :param str custom_registries: juju config for custom registries
    :param str old_custom_registries: old juju config for custom registries
    :return: List Dictionary merged registries
    """
    registries = []
    registries += _registries_list(custom_registries, default=[])
    # json string already converted to python list here
    registries = populate_host_for_custom_registries(registries)
    registries = insert_docker_io_to_custom_registries(registries)
    old_registries = []
    if old_custom_registries:
        old_registries += _registries_list(old_custom_registries, default=[])
    update_custom_tls_config(config_directory, registries, old_registries)

    docker_registry = DB.get("registry", None)
    if docker_registry:
        registries.append(docker_registry)

    return registries


def invalid_custom_registries(custom_registries):
    """
    Validate custom registries from config.

    :param str custom_registries: juju config for custom registries
    :return: error string for blocked status if condition exists, None otherwise
    :rtype: Optional[str]
    """
    try:
        registries = _registries_list(custom_registries)
    except json.JSONDecodeError:
        log(traceback.format_exc())
        return "Failed to decode json string"
    except InvalidCustomRegistriesError:
        log(traceback.format_exc())
        return "custom_registries is not a list"

    required_fields = ["url"]
    str_fields = [
        "url",
        "host",
        "username",
        "password",
        "ca_file",
        "cert_file",
        "key_file",
    ]
    truthy_fields = [
        "insecure_skip_verify",
    ]
    host_set = set()
    for idx, registry in enumerate(registries):
        if not isinstance(registry, dict):
            return "registry #{} is not in object form".format(idx)
        for field in required_fields:
            if field not in registry:
                return "registry #{} missing required field {}".format(idx, field)
        for field in required_fields + str_fields:
            value = registry.get(field)
            if value and not isinstance(value, str):
                return "registry #{} field {}={} is not a string".format(idx, field, value)
        for field in truthy_fields:
            value = registry.get(field)
            if field in registry and not isinstance(value, bool):
                return "registry #{} field {}='{}' is not a boolean".format(idx, field, value)
        for field in registry:
            if field not in str_fields + truthy_fields:
                return "registry #{} field {} may not be specified".format(idx, field)

        this_host = registry.get("host") or strip_url(registry["url"])
        if this_host in host_set:
            return "registry #{} defines {} more than once".format(idx, this_host)
        host_set.add(this_host)


@hook("update-status")
def update_status():
    """
    Triggered when update-status is called.

    :return: None
    """
    if _juju_proxy_changed():
        set_state("containerd.juju-proxy.changed")


@hook("upgrade-charm")
def upgrade_charm():
    """
    Triggered when upgrade-charm is called.

    :return: None
    """
    # Prevent containerd apt pkg from being implicitly updated.
    apt_hold(CONTAINERD_PACKAGE)

    # Re-render config in case the template has changed in the new charm.
    config_changed()

    # Clean up old nvidia sources.list.d files
    old_source_files = [
        "/etc/apt/sources.list.d/nvidia-container-runtime.list",
        "/etc/apt/sources.list.d/cuda.list",
    ]
    for source_file in old_source_files:
        if os.path.exists(source_file):
            os.remove(source_file)
            remove_state("containerd.nvidia.ready")

    # Update containerd version
    clear_flag("containerd.version-published")


@when_not("containerd.br_netfilter.enabled")
def enable_br_netfilter_module():
    """
    Enable br_netfilter to work around https://github.com/kubernetes/kubernetes/issues/21613.

    :return: None
    """
    try:
        modprobe("br_netfilter", persist=True)
    except Exception:
        log(traceback.format_exc())
        if host.is_container():
            log("LXD detected, ignoring failure to load br_netfilter")
        else:
            log("LXD not detected, will retry loading br_netfilter")
            return
    set_state("containerd.br_netfilter.enabled")


@when_not("containerd.ready", "containerd.installed", "endpoint.containerd.departed")
def install_containerd():
    """
    Install containerd and then create initial configuration.

    :return: None
    """
    status.maintenance("Installing containerd via apt")
    apt_update()
    apt_install(CONTAINERD_PACKAGE, fatal=True)
    apt_hold(CONTAINERD_PACKAGE)

    set_state("containerd.installed")
    clear_state("containerd.resource.installed")
    config_changed()


@when("containerd.installed")
@when_not("containerd.resource.installed")
def install_containerd_resource():
    try:
        bin_path = containerd.unpack_containerd_resource()
    except containerd.ResourceFailure as e:
        log("An error occurred extracting the resource")
        log(traceback.format_exc())
        status.blocked(str(e))
        return

    if bin_path is None:
        log("An empty tar.gz resource was providing, using deb sources")
        set_state("containerd.resource.installed")
        return

    for bin in bin_path.glob("./*"):
        check_call(["install", bin, "/usr/bin/"])

    set_state("containerd.resource.installed")


@when("containerd.installed")
@when_not("containerd.version-published")
def publish_version_to_juju():
    """
    Publish the containerd version to Juju.

    :return: None
    """
    output = _check_containerd()
    if not output:
        return

    output = output.decode()
    ver_re = re.compile(r"\s*Version:\s+([\d\.]+)")
    version_matches = set(m.group(1) for m in (ver_re.match(line) for line in output.split("\n")) if m)
    if len(version_matches) != 1:
        return
    (version,) = version_matches

    application_version_set(version)
    set_state("containerd.version-published")


@when_not("containerd.nvidia.checked")
@when_not("endpoint.containerd.departed")
def check_for_gpu():
    """
    Check if an Nvidia GPU exists.

    :return: None
    """
    valid_options = ["auto", "none", "nvidia"]

    driver_config = config().get("gpu_driver")
    if driver_config not in valid_options:
        set_state("containerd.nvidia.invalid-option")
        return

    out = check_output(["lspci", "-nnk"]).rstrip().decode("utf-8").lower()
    nvidia_pci, auto = out.count("nvidia"), driver_config == "auto"

    if driver_config == "none" or (auto and not nvidia_pci):
        # prevent/remove nvidia driver from activating
        # because of config or no nvidia hardware found
        remove_state("containerd.nvidia.available")

    if driver_config == "nvidia" or (auto and nvidia_pci):
        # allow/install nvidia drivers to activate
        # because of config or this found nvidia hardware
        set_state("containerd.nvidia.available")

    remove_state("containerd.nvidia.invalid-option")
    set_state("containerd.nvidia.checked")


@when("containerd.nvidia.ready")
@when_not("containerd.nvidia.available")
def unconfigure_nvidia(reconfigure=True):
    """
    Based on charm config, remove NVIDIA drivers.

    :return: None
    """
    status.maintenance("Removing NVIDIA drivers.")

    nvidia_packages = config("nvidia_apt_packages").split()
    to_purge = apt_packages(nvidia_packages).keys()

    if to_purge:
        apt_purge(to_purge, fatal=True)

    if os.path.isfile(NVIDIA_SOURCES_FILE):
        os.remove(NVIDIA_SOURCES_FILE)

    if to_purge:
        apt_autoremove(purge=True, fatal=True)

    remove_state("containerd.nvidia.ready")
    if reconfigure:
        config_changed()


@when("containerd.nvidia.available", "config.changed.nvidia_apt_key_urls")
def configure_nvidia_sources():
    """Configure NVIDIA repositories based on charm config.

    :return: bool - True if successufully fetched
    """
    status.maintenance("Configuring NVIDIA repositories.")

    dist = host.lsb_release()
    os_release_id = dist["DISTRIB_ID"].lower()
    os_release_version_id = dist["DISTRIB_RELEASE"]
    os_release_version_id_no_dot = os_release_version_id.replace(".", "")

    key_urls = config("nvidia_apt_key_urls").split()
    formatted_key_urls = [
        key_url.format(
            id=os_release_id,
            version_id=os_release_version_id,
            version_id_no_dot=os_release_version_id_no_dot,
        )
        for key_url in key_urls
    ]
    if formatted_key_urls:
        fetched_keys = fetch_url_text(formatted_key_urls)
        if not all(fetched_keys):
            set_state("containerd.nvidia.fetch_keys_failed")
            return False
        remove_state("containerd.nvidia.fetch_keys_failed")

        for key in fetched_keys:
            import_key(key)

    if os.path.isfile(NVIDIA_SOURCES_FILE):
        os.remove(NVIDIA_SOURCES_FILE)

    sources = config("nvidia_apt_sources").splitlines()
    formatted_sources = [
        source.format(
            id=os_release_id,
            version_id=os_release_version_id,
            version_id_no_dot=os_release_version_id_no_dot,
        )
        for source in sources
    ]
    with open(NVIDIA_SOURCES_FILE, "w") as f:
        f.write("\n".join(formatted_sources))

    return True


@when("containerd.nvidia.available")
@when_not("containerd.nvidia.ready", "endpoint.containerd.departed")
def install_nvidia_drivers(reconfigure=True):
    """Based on charm config, install and configure NVIDIA drivers.

    :return: None
    """
    # Fist remove any existing nvidia drivers
    unconfigure_nvidia(reconfigure=False)
    if not configure_nvidia_sources():
        return

    status.maintenance("Installing NVIDIA drivers.")
    apt_update()
    nvidia_packages = config("nvidia_apt_packages").split()
    if not nvidia_packages:
        set_state("containerd.nvidia.missing_package_list")
        return
    remove_state("containerd.nvidia.missing_package_list")

    apt_install(nvidia_packages, fatal=True)
    _test_gpu_reboot()

    set_state("containerd.nvidia.ready")
    if reconfigure:
        config_changed()


@when("endpoint.containerd.departed")
def purge_containerd():
    """
    Purge Containerd from the cluster.

    :return: None
    """
    status.maintenance("Removing containerd from principal")

    host.service_stop("containerd.service")
    apt_unhold(CONTAINERD_PACKAGE)
    apt_purge(CONTAINERD_PACKAGE, fatal=True)

    if is_state("containerd.nvidia.ready"):
        unconfigure_nvidia(reconfigure=False)

    apt_autoremove(purge=True, fatal=True)

    remove_state("containerd.ready")
    remove_state("containerd.installed")
    remove_state("containerd.nvidia.ready")
    remove_state("containerd.nvidia.checked")
    remove_state("containerd.nvidia.available")
    remove_state("containerd.version-published")


@when("config.changed.gpu_driver")
def gpu_config_changed():
    """
    Remove the GPU checked state when the config is changed.

    :return: None
    """
    remove_state("containerd.nvidia.checked")


CONFIG_DIRECTORY = "/etc/containerd"
CONFIG_FILE = "config.toml"


@when("config.changed")
@when_not("endpoint.containerd.departed")
def config_changed():
    """
    Render the config template.

    :return: None
    """
    if _juju_proxy_changed():
        set_state("containerd.juju-proxy.changed")

    # Create "dumb" context based on Config to avoid triggering config.changed
    context = dict(config())
    if context["config_version"] == "v2":
        template_config = "config_v2.toml"
    else:
        template_config = "config.toml"

    endpoint = endpoint_from_flag("endpoint.containerd.available")
    if endpoint:
        sandbox_image = endpoint.get_sandbox_image()
        if sandbox_image:
            log("Setting sandbox_image to: {}".format(sandbox_image))
            context["sandbox_image"] = sandbox_image
        else:
            context["sandbox_image"] = containerd.get_sandbox_image()
    else:
        context["sandbox_image"] = containerd.get_sandbox_image()

    if not os.path.isdir(CONFIG_DIRECTORY):
        os.mkdir(CONFIG_DIRECTORY)

    # If custom_registries changed, make sure to remove old tls files.
    if config().changed("custom_registries"):
        old_custom_registries = config().previous("custom_registries")
    else:
        old_custom_registries = None

    # validate custom_registries
    invalid_reason = invalid_custom_registries(context["custom_registries"])
    if invalid_reason:
        status.blocked("Invalid custom_registries: {}".format(invalid_reason))
        return

    context["custom_registries"] = merge_custom_registries(
        CONFIG_DIRECTORY, context["custom_registries"], old_custom_registries
    )

    untrusted = DB.get("untrusted")
    if untrusted:
        context["untrusted"] = True
        context["untrusted_name"] = untrusted["name"]
        context["untrusted_path"] = untrusted["binary_path"]
        context["untrusted_binary"] = os.path.basename(untrusted["binary_path"])

    else:
        context["untrusted"] = False

    if context.get("runtime") == "auto":
        if is_state("containerd.nvidia.available"):
            context["runtime"] = "nvidia-container-runtime"
        else:
            context["runtime"] = "runc"

    render(template_config, os.path.join(CONFIG_DIRECTORY, CONFIG_FILE), context)

    set_state("containerd.restart")


@when("containerd.installed")
@when("containerd.juju-proxy.changed")
@when_not("endpoint.containerd.departed")
def proxy_changed():
    """
    Apply new proxy settings.

    :return: None
    """
    # Create "dumb" context based on Config
    # to avoid triggering config.changed.
    context = check_for_juju_https_proxy(config)

    service_file = "proxy.conf"
    service_directory = "/etc/systemd/system/containerd.service.d"
    service_path = os.path.join(service_directory, service_file)

    if context.get("http_proxy") or context.get("https_proxy") or context.get("no_proxy"):

        os.makedirs(service_directory, exist_ok=True)

        log("Proxy changed, writing new file to {}".format(service_path))
        render(service_file, service_path, context)

    else:
        try:
            log("Proxy cleaned, removing file {}".format(service_path))
            os.remove(service_path)
        except FileNotFoundError:
            return  # We don't need to restart the daemon.

    DB.set("config-cache", context)

    remove_state("containerd.juju-proxy.changed")
    check_call(["systemctl", "daemon-reload"])
    set_state("containerd.restart")


@when("containerd.restart")
@when_not("endpoint.containerd.departed")
def restart_containerd():
    """
    Restart the containerd service.

    If the restart fails, this function will log a message and be retried on
    the next hook.
    """
    status.maintenance("Restarting containerd")
    if host.service_restart("containerd.service"):
        remove_state("containerd.restart")
    else:
        log("Failed to restart containerd; will retry")


@when("containerd.ready")
@when("endpoint.containerd.joined")
@when_not("endpoint.containerd.departed")
def publish_config():
    """
    Pass configuration to principal charm.

    :return: None
    """
    endpoint = endpoint_from_flag("endpoint.containerd.joined")
    endpoint.set_config(
        socket="unix:///var/run/containerd/containerd.sock",
        runtime="remote",  # TODO handle in k8s worker.
        nvidia_enabled=is_state("containerd.nvidia.available"),
    )


@when("endpoint.untrusted.available")
@when_not("untrusted.configured")
@when_not("endpoint.containerd.departed")
def untrusted_available():
    """
    Handle untrusted container runtime.

    :return: None
    """
    untrusted_runtime = endpoint_from_flag("endpoint.untrusted.available")
    received = dict(untrusted_runtime.get_config())

    if "name" not in received.keys():
        return  # Try until config is available.

    DB.set("untrusted", received)
    config_changed()

    set_state("untrusted.configured")


@when("endpoint.untrusted.departed")
def untrusted_departed():
    """
    Handle untrusted container runtime.

    :return: None
    """
    DB.unset("untrusted")
    DB.flush()
    config_changed()

    remove_state("untrusted.configured")


@when("endpoint.docker-registry.ready")
@when_not("containerd.registry.configured")
def configure_registry():
    """
    Add docker registry config when present.

    :return: None
    """
    registry = endpoint_from_flag("endpoint.docker-registry.ready")

    docker_registry = {
        "host": strip_url(registry.registry_netloc),
        "url": registry.registry_netloc,
    }

    # Handle auth data.
    if registry.has_auth_basic():
        docker_registry["username"] = registry.basic_user
        docker_registry["password"] = registry.basic_password

    # Handle TLS data.
    if registry.has_tls():
        # Ensure the CA that signed our registry cert is trusted.
        host.install_ca_cert(registry.tls_ca, name="juju-docker-registry")

        docker_registry["ca"] = str(ca_crt_path)
        docker_registry["key"] = str(server_key_path)
        docker_registry["cert"] = str(server_crt_path)

    DB.set("registry", docker_registry)

    config_changed()
    set_state("containerd.registry.configured")


@when("endpoint.docker-registry.changed", "containerd.registry.configured")
def reconfigure_registry():
    """
    Signal to update the registry config when something changes.

    :return: None
    """
    remove_state("containerd.registry.configured")


@when("endpoint.containerd.reconfigure")
@when_not("endpoint.containerd.departed")
def container_runtime_relation_changed():
    """
    Run config_changed to use any new config from the endpoint.

    :return: None
    """
    config_changed()
    endpoint = endpoint_from_flag("endpoint.containerd.reconfigure")
    endpoint.handle_remote_config()


@when("containerd.registry.configured")
@when_not("endpoint.docker-registry.joined")
def remove_registry():
    """
    Remove registry config when the registry is no longer present.

    :return: None
    """
    docker_registry = DB.get("registry", None)

    if docker_registry:
        # Remove from DB.
        DB.unset("registry")
        DB.flush()

        # Remove auth-related data.
        log("Disabling auth for docker registry: {}.".format(docker_registry["url"]))

    config_changed()
    remove_state("containerd.registry.configured")
