import os
import json
import requests
import traceback

from subprocess import check_call, check_output, CalledProcessError

from charms.apt import purge

from charms.reactive import endpoint_from_flag
from charms.reactive import (
    when, when_not, when_any, set_state, is_state, remove_state
)

from charms.layer.container_runtime_common import manage_registry_certs

from charmhelpers.core import host
from charmhelpers.core import unitdata
from charmhelpers.core.templating import render
from charmhelpers.core.host import install_ca_cert
from charmhelpers.core.hookenv import status_set, config, log

from charmhelpers.core.kernel import modprobe

from charmhelpers.fetch import apt_install, apt_update, import_key


DB = unitdata.kv()


def _check_containerd():
    """
    Check that containerd is running.

    :returns: Boolean
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

    :returns: None
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


@when('apt.installed.containerd')
@when_not('containerd.ready')
def install_containerd():
    """
    Actual install is handled
    by `layer-apt`.  We'll just
    configure it.

    :returns: None
    """
    config_changed()


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
@when_not('containerd.nvidia.ready')
def configure_nvidia():
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

    apt_install([
        'cuda-drivers',
        'nvidia-container-runtime',
    ], fatal=True)

    set_state('containerd.nvidia.ready')


@when('endpoint.containerd.departed')
def purge_containerd():
    """
    Purge Containerd from the
    cluster.

    :returns: None
    """
    purge('containerd')


@when('config.changed.gpu_driver')
def gpu_config_changed():
    """
    Remove the GPU states when the config
    is changed.

    :returns: None
    """
    remove_state('containerd.nvidia.ready')
    remove_state('containerd.nvidia.available')


@when('config.changed')
def config_changed():
    """
    Render the config template
    and restart the service.

    :returns: None
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
        status_set('blocked', 'Container runtime not available.')


@when_any('config.changed.http_proxy', 'config.changed.https_proxy',
          'config.changed.no_proxy')
@when('containerd.ready')
def proxy_changed():
    """
    Apply new proxy settings.

    :returns: None
    """
    # Create "dumb" context based on Config
    # to avoid triggering config.changed.
    context = dict(config())
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
def publish_config():
    """
    Pass configuration to principal
    charm.

    :returns: None
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

    # Handle TLS data
    cert_subdir = registry.registry_netloc
    if registry.has_tls():
        # Ensure the CA that signed our registry cert is trusted.
        install_ca_cert(registry.tls_ca, name='juju-docker-registry')

    manage_registry_certs(cert_subdir, remove=False)
    # todo: add certs to config https://github.com/containerd/cri/blob/master/docs/registry.md

    # Handle auth data.
    if registry.has_auth_basic():
        #  Set docker registry with auth.
        docker_registry = {
            "url": registry.registry_netloc,
            "username": registry.basic_user,
            "password": registry.basic_password
        }
    else:
        #  Set docker registry without auth.
        docker_registry = {"url": registry.registry_netloc}

    DB.set('registry', docker_registry)

    set_state('config.changed')
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

        # Remove tls-related data.
        cert_subdir = docker_registry['url']

        # Remove auth-related data.
        log('Disabling auth for docker registry: {}.'.format(
            docker_registry['url']))
        manage_registry_certs(cert_subdir, remove=True)

    set_state('config.changed')
    remove_state('containerd.registry.configured')
