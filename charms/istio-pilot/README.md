## Charmed istio-pilot

This charm deploys the control plane for [Istio](https://istio.io), as well as serves as a way for charms to interact with an Istio `Gateway` and obtain `VirtualServices`.  This was designed for use with [Charmed Kubeflow](https://charmed-kubeflow.io/).

## Usage

This charm is generally deployed alongside the `istio-gateway` charm as follows:

```bash
juju deploy istio-pilot --trust 
juju deploy istio-gateway --trust --config kind=ingress istio-ingressgateway
juju relate istio-pilot istio-ingressgateway
```

## Upgrading istio-pilot

### Summary and Limitations

This charm supports in-place upgrades of the Istio control plane by up to one minor version at a time, based on [the upgrade prerequisites recommended by Istio](https://istio.io/latest/docs/setup/upgrade/in-place/#upgrade-prerequisites).  Downgrades are not supported by this charm.  For example:
* Supported
  * Upgrade from Istio 1.15.0 to 1.15.1
  * Upgrade from Istio 1.15.0 to Istio 1.16.1
  * Upgrade from Istio 1.16.0 to Istio 1.16.0 (although this may be a no-op)
* Unsupported
    * Upgrade more than one minor version at a time, from Istio 1.13.0 to 1.15.0
    * Downgrade from Istio 1.15.0 to 1.14.0

The charm will attempt to validate the upgrade prior to modifying the control plane, setting an `ErrorStatus` and logging the reason if an unsupported upgrade is detected.

When upgrading istio-pilot, you should also upgrade istio-gateway at the same time to the same version.  These charms are only tested against each other at the same version.

If you need to upgrade across multiple versions (say from Istio 1.11.0 to Istio 1.16.0), refresh the charm one minor version at a time.  For example, starting with a deployed istio-pilot 1.11, you can do:
* `juju refresh istio-pilot --channel 1.12/stable`
* `juju refresh istio-pilot --channel 1.13/stable`
* `juju refresh istio-pilot --channel 1.14/stable`
* `juju refresh istio-pilot --channel 1.15/stable`
* `juju refresh istio-pilot --channel 1.16/stable`

Where between each command you wait until the upgrade is complete.

### Debugging Failed Upgrades

The sections below describe different scenarios for failed upgrades.  In general, the debugging procedure is:
* if you're trying to upgrade across version gaps that are not supported (eg: two minor versions), refresh the charm back to a supported version
* if something unexpected happens, use the [istioctl](https://istio.io/latest/docs/reference/commands/istioctl/) and debugging guidance from [Istio](https://istio.io/latest/) to diagnose and resolve any issues.  If you can restore the cluster to a running state that is at most one minor version behind your target version, you can then `juju resolved istio-pilot` to get Istio to retry the upgrade. 

If at any point you use the [istioctl](https://istio.io/latest/docs/reference/commands/istioctl/) tool to manually purge Istio from your cluster, it is recommended that you also remove the `istio-gateway` charm and redeploy it as needed.  

#### Upgrades across more than one minor version

When the charm detects an upgrade across more than one minor version, it will set an `ErrorStatus` and log the reason.  For example, if you have istio-pilot 1.13.0 deployed and you `juju refresh istio-pilot --channel 1.15/stable`, the upgrade-charm event will fail and the charm will go to `ErrorStatus`.  To resolve this error, refresh the charm again back to a supported version (`juju refresh istio-pilot --channel 1.14/stable`).  The charm should recover and successfully upgrade Istio to 1.14.  You can then do a second refresh to get to Istio 1.15.    

If for some reason you cannot use the charm's typical upgrade procedure to get to within one minor version of your target version, you can use the [istioctl](https://istio.io/latest/docs/reference/commands/istioctl/) tool to manually upgrade through some versions.  

#### Downgrades

If you accidentally attempt a downgrade, the istio-pilot charm will go to error state.  To resolve, `juju refresh` istio-pilot to a new charm version that is at or above the installed version of istio.

#### Unknown version errors

If the upgrade fails saying it cannot find the control plane version, this likely means your existing istio deployment is missing key pieces.  Use the [istioctl](https://istio.io/latest/docs/reference/commands/istioctl/) tool to inspect further. 
