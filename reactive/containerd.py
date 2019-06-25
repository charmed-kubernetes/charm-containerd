import os
import json
import requests

from subprocess import check_call, check_output, CalledProcessError

from charms.apt import purge

from charms.reactive import endpoint_from_flag
from charms.reactive import (
    when, when_not, when_any, set_state, is_state, remove_state
)

from charmhelpers.core import host
from charmhelpers.core.templating import render
from charmhelpers.core.hookenv import status_set, config, log

from charmhelpers.core.kernel import modprobe

from charmhelpers.fetch import apt_install, apt_update, import_key


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

    # Mutate the input string into a dictionary.
    context['custom_registries'] = \
        json.loads(context['custom_registries'])

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


@when_not('containerd.br_netfilter.enabled')
def enable_br_netfilter_module():
    # Fixes https://github.com/kubernetes/kubernetes/issues/21613
    modprobe('br_netfilter', persist=True)
    set_state('containerd.br_netfilter.enabled')
