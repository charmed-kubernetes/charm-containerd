import os
import json
import requests
import traceback

from subprocess import (
    check_call,
    check_output,
    CalledProcessError
)

from charms.reactive import endpoint_from_flag
from charms.reactive import (
    when,
    when_not,
    when_any,
    set_state,
    is_state,
    remove_state
)

from charms.layer.container_runtime_common import (
    ca_crt_path,
    server_crt_path,
    server_key_path,
    check_for_juju_https_proxy
)

from charmhelpers.core import host
from charmhelpers.core import unitdata
from charmhelpers.core.templating import render
from charmhelpers.core.host import install_ca_cert
from charmhelpers.core.hookenv import (
    log,
    config,
    status_set,
    env_proxy_settings
)

from charmhelpers.core.kernel import modprobe
from charmhelpers.core.templating import render
from charmhelpers.core.hookenv import (
    log,
    config,
    status_set,
    resource_get
)

from charmhelpers.fetch import (
    apt_install,
    apt_update,
    apt_purge,
    apt_autoremove,
    import_key
)

DB = unitdata.kv()
KATA_PACKAGES = [
    'kata-runtime',
    'kata-proxy',
    'kata-shim'
]
NVIDIA_PACKAGES = [
    'cuda-drivers',
    'nvidia-container-runtime'
]


def _check_containerd():
    """
    Check that containerd is running.

    :return: Boolean
    """
    try:
        check_call([
            'ctr',
            'c',
            'ls'
        ])
    except (FileNotFoundError, CalledProcessError):
        return False

    return True


def merge_custom_registries(custom_registries):
    """
    Merge custom registries and Docker
    registries from relation.

    :return: List Dictionary merged registries
    """
    registries = []
    registries += json.loads(custom_registries)

    docker_registry = DB.get('registry', None)
    if docker_registry:
        registries.append(docker_registry)

    return registries


@when_not('containerd.br_netfilter.enabled')
def enable_br_netfilter_module():
    """
    Enable br_netfilter to work around
    https://github.com/kubernetes/kubernetes/issues/21613

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


def install_kata():
    """
    Install the Kata container runtime.

    :returns: None
    """
    dist = host.lsb_release()
    release = '{}_{}'.format(
        dist['DISTRIB_ID'],
        dist['DISTRIB_RELEASE']
    )

    arch = check_output(['arch']).decode().strip()

    archive = resource_get('kata-archive')

    if not archive or os.path.getsize(archive) == 0:
        status_set('maintenance', 'Installing Kata via apt')
        gpg_key = requests.get(
            'http://download.opensuse.org/repositories/home:/katacontainers:/'
            'releases:/{}:/master/x{}/Release.key'.format(arch, release)).text
        import_key(gpg_key)

        with open('/etc/apt/sources.list.d/kata-containers.list', 'w') as f:
            f.write(
                'deb http://download.opensuse.org/repositories/home:/'
                'katacontainers:/releases:/{}:/master/x{}/ /'
                .format(arch, release)
            )

        apt_update()
        apt_install(KATA_PACKAGES)

    else:
        status_set('maintenance', 'Installing Kata via resource')
        unpack = '/tmp/kata-debs'

        if not os.path.isdir(unpack):
            os.makedirs(unpack, exist_ok=True)

        check_call(['tar', '-xvf', archive, '-C', unpack])
        check_call('apt-get install -y {}/*.deb'.format(unpack), shell=True)

    set_state('kata.installed')


def purge_kata():
    """
    Purge Kata containers.

    :return: None
    """
    if is_state('kata.installed'):
        apt_purge(KATA_PACKAGES, fatal=True)

    source = '/etc/apt/sources.list.d/kata-containers.list'
    if os.path.isfile(source):
        os.remove(source)

    remove_state('kata.installed')


@when_not('containerd.ready',
          'containerd.installed',
          'endpoint.containerd.departed')
def install_containerd():
    """
    Check to see if there's an archive
    attached.  If so, unpack and install.
    If not, install via apt.

    :return: None
    """
    archive = resource_get('containerd-archive')

    if not archive or os.path.getsize(archive) == 0:
        status_set('maintenance', 'Installing containerd via apt')
        apt_update()
        apt_install('containerd', fatal=True)

    else:
        status_set('maintenance', 'Installing containerd via resource')
        unpack = '/tmp/containerd-debs'

        if not os.path.isdir(unpack):
            os.makedirs(unpack, exist_ok=True)

        check_call(['tar', '-xvf', archive, '-C', unpack])
        check_call('apt-get install -y {}/*.deb'.format(unpack), shell=True)

    if _check_containerd():
        status_set('active', 'Container runtime available.')
        set_state('containerd.installed')
        config_changed()
    else:
        status_set('blocked', 'Container runtime not available.')


@when('config.enable-kata.changed')
def change_kata():
    """
    Install or remove Kata containers.

    :return: None
    """
    context = dict(config())
    enabled = context['enable-kata']

    if enabled == 'true':
        install_kata()
    elif enabled == 'false':
        purge_kata()
    else:
        status_set('blocked', 'enable-kata is invalid ({})'.format(enabled))


@when_not('containerd.nvidia.ready')
@when_not('containerd.nvidia.available')
def check_for_gpu():
    """
    Check if an Nvidia GPU
    exists.
    """
    valid_options = [
        'auto',
        'none',
        'nvidia'
    ]

    driver_config = config().get('gpu_driver')
    if driver_config not in valid_options:
        status_set(
            'blocked',
            '{} is an invalid option for gpu_driver'.format(
                driver_config
            )
        )
        return

    out = check_output(['lspci', '-nnk']).rstrip().decode('utf-8').lower()

    if driver_config != 'none':
        if (out.count('nvidia') > 0 and driver_config == 'auto') \
                or (driver_config == 'nvidia'):
            set_state('containerd.nvidia.available')


@when('containerd.nvidia.available')
@when_not('containerd.nvidia.ready', 'endpoint.containerd.departed')
def configure_nvidia():
    """
    Install and configure Nvidia drivers.

    :return: None
    """
    status_set('maintenance', 'Installing Nvidia drivers.')

    dist = host.lsb_release()
    release = '{}{}'.format(
        dist['DISTRIB_ID'].lower(),
        dist['DISTRIB_RELEASE']
    )

    ncr_gpg_key = requests.get(
        'https://nvidia.github.io/nvidia-container-runtime/gpgkey').text
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
        .format(release.replace('.', ''))
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
    Purge Containerd from the
    cluster.

    :return: None
    """
    status_set('maintenance', 'Removing containerd from principal')

    host.service_stop('containerd.service')
    apt_purge('containerd', fatal=True)

    if is_state('containerd.nvidia.ready'):
        apt_purge(NVIDIA_PACKAGES, fatal=True)

    sources = [
        '/etc/apt/sources.list.d/cuda.list',
        '/etc/apt/sources.list.d/nvidia-container-runtime.list',
    ]

    for f in sources:
        if os.path.isfile(f):
            os.remove(f)

    purge_kata()

    apt_autoremove(purge=True, fatal=True)

    remove_state('containerd.ready')
    remove_state('containerd.installed')
    remove_state('containerd.nvidia.ready')
    remove_state('containerd.nvidia.available')


@when('config.changed.gpu_driver')
def gpu_config_changed():
    """
    Remove the GPU states when the config
    is changed.

    :return: None
    """
    remove_state('containerd.nvidia.ready')
    remove_state('containerd.nvidia.available')


@when('config.changed')
@when_not('endpoint.containerd.departed')
def config_changed():
    """
    Render the config template
    and restart the service.

    :return: None
    """
    # Create "dumb" context based on Config
    # to avoid triggering config.changed.
    context = dict(config())

    config_file = 'config.toml'
    config_directory = '/etc/containerd'

    context['custom_registries'] = \
        merge_custom_registries(context['custom_registries'])

    if is_state('containerd.nvidia.available') \
            and context.get('runtime') == 'auto':
        context['runtime'] = 'nvidia-container-runtime'
    else:
        context['runtime'] = 'runc'

    if not os.path.isdir(config_directory):
        os.mkdir(config_directory)

    render(
        config_file,
        os.path.join(config_directory, config_file),
        context
    )

    log('Restarting containerd.service')
    host.service_restart('containerd.service')

    if _check_containerd():
        status_set('active', 'Container runtime available.')
        set_state('containerd.ready')

    else:
        if not is_state('containerd.installed'):
            remove_state('containerd.ready')
        else:
            status_set('blocked', 'Container runtime not available.')


@when('containerd.ready')
@when_any('config.changed.http_proxy', 'config.changed.https_proxy',
          'config.changed.no_proxy')
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

        render(
            service_file,
            service_path,
            context
        )

    else:
        try:
            os.remove(service_path)
        except FileNotFoundError:
            return  # We don't need to restart the daemon.

    check_call(['systemctl', 'daemon-reload'])
    log('Restarting containerd.service')
    host.service_restart('containerd.service')


@when('containerd.ready')
@when('endpoint.containerd.joined')
@when_not('endpoint.containerd.departed')
def publish_config():
    """
    Pass configuration to principal
    charm.

    :return: None
    """
    endpoint = endpoint_from_flag('endpoint.containerd.joined')
    endpoint.set_config(
        socket='unix:///var/run/containerd/containerd.sock',
        runtime='remote',  # TODO handle in k8s worker.
        nvidia_enabled=is_state('containerd.nvidia.ready')
    )


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
        install_ca_cert(registry.tls_ca, name='juju-docker-registry')

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
