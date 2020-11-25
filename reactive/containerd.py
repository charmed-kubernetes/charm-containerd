import os
import base64
import binascii
import json
import requests
import traceback

from subprocess import (
    check_call,
    check_output,
    CalledProcessError
)

from charms.reactive import (
    hook,
    when,
    when_not,
    set_state,
    is_state,
    remove_state,
    endpoint_from_flag
)

from charms.layer import containerd, status
from charms.layer.container_runtime_common import (
    ca_crt_path,
    server_crt_path,
    server_key_path,
    check_for_juju_https_proxy
)

from charmhelpers.core import (
    host,
    unitdata
)

from charmhelpers.core.templating import render
from charmhelpers.core.hookenv import (
    atexit,
    config,
    log,
    application_version_set
)

from charmhelpers.core.kernel import modprobe

from charmhelpers.fetch import (
    apt_install,
    apt_update,
    apt_purge,
    apt_hold,
    apt_autoremove,
    apt_unhold,
    import_key
)


DB = unitdata.kv()

CONTAINERD_PACKAGE = 'containerd'

NVIDIA_PACKAGES = [
    'cuda-drivers',
    'nvidia-container-runtime',
]


def _check_containerd():
    """
    Check that containerd is running.

    `ctr version` calls both client and server side, so is a reasonable indication that everything's been set up
    correctly.

    :return: Boolean
    """
    try:
        version = check_output(['ctr', 'version'])
    except (FileNotFoundError, CalledProcessError):
        return None

    return version


def _juju_proxy_changed():
    """
    Check to see if the Juju model HTTP(S) proxy settings have changed.

    These aren't propagated to the charm so we'll need to do it here.

    :return: Boolean
    """
    cached = DB.get('config-cache', None)
    if not cached:
        return True  # First pass.

    new = check_for_juju_https_proxy(config)

    if cached['http_proxy'] == new['http_proxy'] and \
            cached['https_proxy'] == new['https_proxy'] and \
            cached['no_proxy'] == new['no_proxy']:
        return False

    return True


@atexit
def charm_status():
    """
    Set the charm's status after each hook is run.

    :return: None
    """
    if is_state('upgrade.series.in-progress'):
        status.blocked('Series upgrade in progress')
    elif is_state('containerd.nvidia.invalid-option'):
        status.blocked(
            '{} is an invalid option for gpu_driver'.format(
                config().get('gpu_driver')
            )
        )
    elif _check_containerd():
        status.active('Container runtime available')
        set_state('containerd.ready')
    else:
        status.blocked('Container runtime not available')


def strip_url(url):
    """Strip the URL of protocol, slashes etc., and keep host:port.

    Examples:
        url: http://10.10.10.10:8000 --> return: 10.10.10.10:8000
        url: https://myregistry.io:8000/ --> return: myregistry.io:8000
        url: myregistry.io:8000 --> return: myregistry.io:8000
    """
    return url.rstrip('/').split(sep='://', maxsplit=1)[-1]


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
        for opt in ['ca', 'key', 'cert']:
            file_b64 = registry.get('%s_file' % opt)
            if file_b64:
                registry[opt] = os.path.join(
                    config_directory, "%s.%s" % (strip_url(registry['url']), opt)
                )
                if os.path.isfile(registry[opt]):
                    os.remove(registry[opt])

    # Write tls files of new registries.
    for registry in registries:
        for opt in ['ca', 'key', 'cert']:
            file_b64 = registry.get('%s_file' % opt)
            if file_b64:
                try:
                    file_contents = base64.b64decode(file_b64)
                except (binascii.Error, TypeError):
                    log(traceback.format_exc())
                    log("{}:{} didn't look like base64 data... skipping"
                        .format(registry['url'], opt))
                    continue
                registry[opt] = os.path.join(
                    config_directory, "%s.%s" % (strip_url(registry['url']), opt)
                )
                with open(registry[opt], 'wb') as f:
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
            if not registry.get('host'):
                url = registry.get('url')
                if url:
                    registry['host'] = strip_url(url)

    return custom_registries


def merge_custom_registries(config_directory, custom_registries,
                            old_custom_registries):
    """
    Merge custom registries and Docker registries from relation.

    :param str config_directory: containerd config directory
    :param str custom_registries: juju config for custom registries
    :param str old_custom_registries: old juju config for custom registries
    :return: List Dictionary merged registries
    """
    registries = []
    registries += json.loads(custom_registries)
    # json string already converted to python list here
    registries = populate_host_for_custom_registries(registries)
    old_registries = []
    if (old_custom_registries):
        old_registries += json.loads(old_custom_registries)
    update_custom_tls_config(config_directory, registries, old_registries)

    docker_registry = DB.get('registry', None)
    if docker_registry:
        registries.append(docker_registry)

    return registries


@hook('update-status')
def update_status():
    """
    Triggered when update-status is called.

    :return: None
    """
    if _juju_proxy_changed():
        set_state('containerd.juju-proxy.changed')


@hook('upgrade-charm')
def upgrade_charm():
    """
    Triggered when upgrade-charm is called.

    :return: None
    """
    # Prevent containerd apt pkg from being implicitly updated.
    apt_hold(CONTAINERD_PACKAGE)

    # Re-render config in case the template has changed in the new charm.
    config_changed()


@when_not('containerd.br_netfilter.enabled')
def enable_br_netfilter_module():
    """
    Enable br_netfilter to work around https://github.com/kubernetes/kubernetes/issues/21613.

    :return: None
    """
    try:
        modprobe('br_netfilter', persist=True)
    except Exception:
        log(traceback.format_exc())
        if host.is_container():
            log('LXD detected, ignoring failure to load br_netfilter')
        else:
            log('LXD not detected, will retry loading br_netfilter')
            return
    set_state('containerd.br_netfilter.enabled')


@when_not('containerd.ready',
          'containerd.installed',
          'endpoint.containerd.departed')
def install_containerd():
    """
    Install containerd and then create initial configuration.

    :return: None
    """
    status.maintenance('Installing containerd via apt')
    apt_update()
    apt_install(CONTAINERD_PACKAGE, fatal=True)
    apt_hold(CONTAINERD_PACKAGE)

    set_state('containerd.installed')
    config_changed()


@when('containerd.installed')
@when_not('containerd.version-published')
def publish_version_to_juju():
    """
    Publish the containerd version to Juju.

    :return: None
    """
    version_string = _check_containerd()
    if not version_string:
        return
    version = version_string.split()[6].split(b'-')[0].decode()

    application_version_set(version)
    set_state('containerd.version-published')


@when_not('containerd.nvidia.checked')
@when_not('endpoint.containerd.departed')
def check_for_gpu():
    """
    Check if an Nvidia GPU exists.

    :return: None
    """
    valid_options = [
        'auto',
        'none',
        'nvidia'
    ]

    driver_config = config().get('gpu_driver')
    if driver_config not in valid_options:
        set_state('containerd.nvidia.invalid-option')
        return

    out = check_output(['lspci', '-nnk']).rstrip().decode('utf-8').lower()

    if driver_config != 'none':
        if (out.count('nvidia') > 0 and driver_config == 'auto') \
                or (driver_config == 'nvidia'):
            set_state('containerd.nvidia.available')
        else:
            remove_state('containerd.nvidia.available')
            remove_state('containerd.nvidia.ready')

    remove_state('containerd.nvidia.invalid-option')
    set_state('containerd.nvidia.checked')


@when('containerd.nvidia.available')
@when_not('containerd.nvidia.ready', 'endpoint.containerd.departed')
def configure_nvidia():
    """
    Based on charm config, install and configure Nivida drivers.

    :return: None
    """
    status.maintenance('Installing Nvidia drivers.')

    dist = host.lsb_release()
    release = '{}{}'.format(
        dist['DISTRIB_ID'].lower(),
        dist['DISTRIB_RELEASE']
    )
    proxies = {
        "http": config('http_proxy'),
        "https": config('https_proxy')
    }
    ncr_gpg_key = requests.get(
        'https://nvidia.github.io/nvidia-container-runtime/gpgkey', proxies=proxies).text
    import_key(ncr_gpg_key)
    with open(
        '/etc/apt/sources.list.d/nvidia-container-runtime.list', 'w'
    ) as f:
        f.write(
            'deb '
            'https://nvidia.github.io/libnvidia-container/{}/$(ARCH) /\n'
            .format(release)
        )
        f.write(
            'deb '
            'https://nvidia.github.io/nvidia-container-runtime/{}/$(ARCH) /\n'
            .format(release)
        )

    cuda_gpg_key = requests.get(
        'https://developer.download.nvidia.com/'
        'compute/cuda/repos/{}/x86_64/7fa2af80.pub'
        .format(release.replace('.', '')), proxies=proxies
    ).text
    import_key(cuda_gpg_key)
    with open('/etc/apt/sources.list.d/cuda.list', 'w') as f:
        f.write(
            'deb '
            'http://developer.download.nvidia.com/'
            'compute/cuda/repos/{}/x86_64 /\n'
            .format(release.replace('.', ''))
        )

    apt_update()

    apt_install(NVIDIA_PACKAGES, fatal=True)

    set_state('containerd.nvidia.ready')
    config_changed()


@when('endpoint.containerd.departed')
def purge_containerd():
    """
    Purge Containerd from the cluster.

    :return: None
    """
    status.maintenance('Removing containerd from principal')

    host.service_stop('containerd.service')
    apt_unhold(CONTAINERD_PACKAGE)
    apt_purge(CONTAINERD_PACKAGE, fatal=True)

    if is_state('containerd.nvidia.ready'):
        apt_purge(NVIDIA_PACKAGES, fatal=True)

    sources = [
        '/etc/apt/sources.list.d/cuda.list',
        '/etc/apt/sources.list.d/nvidia-container-runtime.list'
    ]

    for f in sources:
        if os.path.isfile(f):
            os.remove(f)

    apt_autoremove(purge=True, fatal=True)

    remove_state('containerd.ready')
    remove_state('containerd.installed')
    remove_state('containerd.nvidia.ready')
    remove_state('containerd.nvidia.checked')
    remove_state('containerd.nvidia.available')
    remove_state('containerd.version-published')


@when('config.changed.gpu_driver')
def gpu_config_changed():
    """
    Remove the GPU checked state when the config is changed.

    :return: None
    """
    remove_state('containerd.nvidia.checked')


@when('config.changed')
@when_not('endpoint.containerd.departed')
def config_changed():
    """
    Render the config template.

    :return: None
    """
    if _juju_proxy_changed():
        set_state('containerd.juju-proxy.changed')

    # Create "dumb" context based on Config to avoid triggering config.changed
    context = dict(config())

    config_file = 'config.toml'
    config_directory = '/etc/containerd'

    endpoint = endpoint_from_flag('endpoint.containerd.available')
    if endpoint:
        sandbox_image = endpoint.get_sandbox_image()
        if sandbox_image:
            log('Setting sandbox_image to: {}'.format(sandbox_image))
            context['sandbox_image'] = sandbox_image
        else:
            context['sandbox_image'] = containerd.get_sandbox_image()
    else:
        context['sandbox_image'] = containerd.get_sandbox_image()

    if not os.path.isdir(config_directory):
        os.mkdir(config_directory)

    # If custom_registries changed, make sure to remove old tls files.
    if config().changed('custom_registries'):
        old_custom_registries = config().previous('custom_registries')
    else:
        old_custom_registries = None

    context['custom_registries'] = \
        merge_custom_registries(config_directory, context['custom_registries'],
                                old_custom_registries)

    untrusted = DB.get('untrusted')
    if untrusted:
        context['untrusted'] = True
        context['untrusted_name'] = untrusted['name']
        context['untrusted_path'] = untrusted['binary_path']
        context['untrusted_binary'] = os.path.basename(
            untrusted['binary_path'])

    else:
        context['untrusted'] = False

    if is_state('containerd.nvidia.available') \
            and context.get('runtime') == 'auto':
        context['runtime'] = 'nvidia-container-runtime'
    if not is_state('containerd.nvidia.available') \
            and context.get('runtime') == 'auto':
        context['runtime'] = 'runc'

    render(
        config_file,
        os.path.join(config_directory, config_file),
        context
    )

    set_state('containerd.restart')


@when('containerd.installed')
@when('containerd.juju-proxy.changed')
@when_not('endpoint.containerd.departed')
def proxy_changed():
    """
    Apply new proxy settings.

    :return: None
    """
    # Create "dumb" context based on Config
    # to avoid triggering config.changed.
    context = check_for_juju_https_proxy(config)

    service_file = 'proxy.conf'
    service_directory = '/etc/systemd/system/containerd.service.d'
    service_path = os.path.join(service_directory, service_file)

    if context.get('http_proxy') or \
            context.get('https_proxy') or context.get('no_proxy'):

        os.makedirs(service_directory, exist_ok=True)

        log('Proxy changed, writing new file to {}'.format(service_path))
        render(
            service_file,
            service_path,
            context
        )

    else:
        try:
            log('Proxy cleaned, removing file {}'.format(service_path))
            os.remove(service_path)
        except FileNotFoundError:
            return  # We don't need to restart the daemon.

    DB.set('config-cache', context)

    remove_state('containerd.juju-proxy.changed')
    check_call(['systemctl', 'daemon-reload'])
    set_state('containerd.restart')


@when('containerd.restart')
@when_not('endpoint.containerd.departed')
def restart_containerd():
    """
    Restart the containerd service.

    If the restart fails, this function will log a message and be retried on
    the next hook.
    """
    status.maintenance('Restarting containerd')
    if host.service_restart('containerd.service'):
        remove_state('containerd.restart')
    else:
        log('Failed to restart containerd; will retry')


@when('containerd.ready')
@when('endpoint.containerd.joined')
@when_not('endpoint.containerd.departed')
def publish_config():
    """
    Pass configuration to principal charm.

    :return: None
    """
    endpoint = endpoint_from_flag('endpoint.containerd.joined')
    endpoint.set_config(
        socket='unix:///var/run/containerd/containerd.sock',
        runtime='remote',  # TODO handle in k8s worker.
        nvidia_enabled=is_state('containerd.nvidia.available')
    )


@when('endpoint.untrusted.available')
@when_not('untrusted.configured')
@when_not('endpoint.containerd.departed')
def untrusted_available():
    """
    Handle untrusted container runtime.

    :return: None
    """
    untrusted_runtime = endpoint_from_flag('endpoint.untrusted.available')
    received = dict(untrusted_runtime.get_config())

    if 'name' not in received.keys():
        return  # Try until config is available.

    DB.set('untrusted', received)
    config_changed()

    set_state('untrusted.configured')


@when('endpoint.untrusted.departed')
def untrusted_departed():
    """
    Handle untrusted container runtime.

    :return: None
    """
    DB.unset('untrusted')
    DB.flush()
    config_changed()

    remove_state('untrusted.configured')


@when('endpoint.docker-registry.ready')
@when_not('containerd.registry.configured')
def configure_registry():
    """
    Add docker registry config when present.

    :return: None
    """
    registry = endpoint_from_flag('endpoint.docker-registry.ready')

    docker_registry = {
        'url': registry.registry_netloc
    }

    # Handle auth data.
    if registry.has_auth_basic():
        docker_registry['username'] = registry.basic_user
        docker_registry['password'] = registry.basic_password

    # Handle TLS data.
    if registry.has_tls():
        # Ensure the CA that signed our registry cert is trusted.
        host.install_ca_cert(registry.tls_ca, name='juju-docker-registry')

        docker_registry['ca'] = str(ca_crt_path)
        docker_registry['key'] = str(server_key_path)
        docker_registry['cert'] = str(server_crt_path)

    DB.set('registry', docker_registry)

    config_changed()
    set_state('containerd.registry.configured')


@when('endpoint.docker-registry.changed',
      'containerd.registry.configured')
def reconfigure_registry():
    """
    Signal to update the registry config when something changes.

    :return: None
    """
    remove_state('containerd.registry.configured')


@when('endpoint.containerd.reconfigure')
@when_not('endpoint.containerd.departed')
def container_runtime_relation_changed():
    """
    Run config_changed to use any new config from the endpoint.

    :return: None
    """
    config_changed()
    endpoint = endpoint_from_flag('endpoint.containerd.reconfigure')
    endpoint.handle_remote_config()


@when('containerd.registry.configured')
@when_not('endpoint.docker-registry.joined')
def remove_registry():
    """
    Remove registry config when the registry is no longer present.

    :return: None
    """
    docker_registry = DB.get('registry', None)

    if docker_registry:
        # Remove from DB.
        DB.unset('registry')
        DB.flush()

        # Remove auth-related data.
        log('Disabling auth for docker registry: {}.'.format(
            docker_registry['url']))

    config_changed()
    remove_state('containerd.registry.configured')
