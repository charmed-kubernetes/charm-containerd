# Charm for Containerd

This subordinate charm deploys the [Containerd](https://containerd.io/)
engine within a running Juju charm application. Containerd is an open platform
for developers and sysadmins to build, ship, and run distributed applications
in containers.

Containerd focuses on distributing applications as containers that can be quickly
assembled from components that are run the same on different servers without
environmental dependencies. This eliminates the friction between development,
QA, and production environments.

# States

The following states are set by this subordinate:

* `endpoint.{relation name}.available`

  This state is set when containerd is available for use.


## Using the Containerd subordinate charm

The Containerd subordinate charm is to be used with principal
charms that need a container runtime.  To use, we deploy
the Containerd subordinate charm and then relate it to the
principal charm.

```
juju deploy cs:~containers/containerd
juju add-relation containerd [principal charm]
```

## Scale out Usage

This charm will automatically scale out with the
principal charm.

# Configuration

See [config.yaml](config.yaml) for
list of configuration options.

> Note: Setting HTTP proxy values will be overriden if `juju-http-proxy` or `juju-https-proxy` settings are set on the model.

# Testing

When upgrading this charm, the [`container_runtime_version`](https://github.com/VariableDeclared/jenkins/blob/3bf2cabc7185568e6a80a137a7f083c428048683/jobs/validate-juju-https-envs.yaml#L25) of the `validate-https-proxy-envs` key needs to be updated with the new containerd version.

# Contact Information

This charm is available at <https://jujucharms.com/containerd> and contains the
open source operations code to deploy on all public clouds in the Juju
ecosystem.

## Containerd links

  - The [Containerd homepage](https://containerd.io/)
