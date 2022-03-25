from charmhelpers.core import hookenv, unitdata


def get_sandbox_image():
    """
    Return the container image location for the sandbox_image.

    Set an appropriate sandbox image based on known registries. Precedence should be:
    - related docker-registry
    - default charmed k8s registry (if related to kubernetes)
    - upstream

    :return: str container image location
    """
    db = unitdata.kv()
    canonical_registry = 'rocks.canonical.com:443/cdk'
    upstream_registry = 'k8s.gcr.io'

    docker_registry = db.get('registry', None)
    if docker_registry:
        sandbox_registry = docker_registry['url']
    else:
        try:
            deployment = hookenv.goal_state()
        except NotImplementedError:
            relations = []
            for rid in hookenv.relation_ids('containerd'):
                relations.append(hookenv.remote_service_name(rid))
        else:
            relations = deployment.get('relations', {}).get('containerd', {})

        # wokeignore:rule=master maintains backward compatability with kubernetes-master
        if any(k in relations for k in ('kubernetes-control-plane', 'kubernetes-master', 'kubernetes-worker')):
            sandbox_registry = canonical_registry
        else:
            sandbox_registry = upstream_registry

    return '{}/pause:3.6'.format(sandbox_registry)
