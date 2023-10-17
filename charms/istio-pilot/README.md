## Charmed istio-pilot

This charm deploys the control plane for [Istio](https://istio.io), as well as serves as a way for charms to interact with an Istio `Gateway` and obtain `VirtualServices`.  This was designed for use with [Charmed Kubeflow](https://charmed-kubeflow.io/).

## Usage

This charm is generally deployed alongside the `istio-gateway` charm as follows:

```bash
juju deploy istio-pilot --trust 
juju deploy istio-gateway --trust --config kind=ingress istio-ingressgateway
juju relate istio-pilot istio-ingressgateway
```
## Enable TLS ingress gateway for a single host

This charm provides means to integrate with TLS certificates providers that help with this configuration. The following can be used as TLS certificates providers depending on the use case or security requirements of an organisation:

* For development or non-production environments, the istio-pilot charm can be related to the [self-signed-certificates-operator](https://github.com/canonical/self-signed-certificates-operator/tree/main).
* For environments where certificates are managed by the organisation, the [tls-certificates-operator](https://github.com/canonical/tls-certificates-operator) can be used.
* For deployments where it is desired to use ACME servers for validation (like Let's encrypt), you can use any of [httpreq-acme-operator](https://github.com/canonical/httpreq-acme-operator), [namecheap-acme-operator](https://github.com/canonical/namecheap-acme-operator), or [route53-acme-operator](https://github.com/canonical/route53-acme-operator).

```bash
juju relate istio-pilot:certificates <TLS certificates providers>:certificates
```

> Please refer to the [Secure your charm deployments with X.509 certificates](https://charmhub.io/topics/secure-your-charm-deployments-with-x-509-certificates) entry to understand the different use cases and TLS certificates providers offerings.

> Please refer to the official documentation for more details about the [TLS ingress gateway for a single host](https://istio.io/latest/docs/tasks/traffic-management/ingress/secure-ingress/#configure-a-tls-ingress-gateway-for-a-single-host).

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

### Typical Upgrade Procedure

Note: Before upgrading istio-pilot, **always remove the istio-ingressgateway application**[^1]

To upgrade Istio by one minor version, the procedure is:
* `juju remove-application istio-ingressgateway`[^1]
* `juju refresh istio-pilot --channel <desired-version>/stable`
* `juju deploy istio-gateway --channel <desired-version>/stable --trust --config kind=ingress istio-ingressgateway`

Where the charm `--config` fields should match whatever you were using before upgrading.   

For upgrades across multiple versions (say from Istio 1.11.0 to Istio 1.16.0), use the same procedure as above but refresh istio-pilot multiple times through each intermediate minor version.  For example, starting with a deployed istio-pilot 1.11, you can do:
* `juju remove-application istio-ingressgateway`[^1]
* `juju refresh istio-pilot --channel 1.12/stable`
* `juju refresh istio-pilot --channel 1.13/stable`
* `juju refresh istio-pilot --channel 1.14/stable`
* `juju refresh istio-pilot --channel 1.15/stable`
* `juju refresh istio-pilot --channel 1.16/stable`
* `juju deploy istio-gateway --channel <desired-version>/stable --trust --config kind=ingress istio-ingressgateway`

Where between each refresh command you wait until the upgrade is complete.

### Debugging Failed Upgrades

The sections below describe different scenarios for failed upgrades.  To get more detailed information about any failed upgrade, see `juju debug-log -i istio-pilot/0` for more verbose logs.  In general, the debugging procedure is:
* if you're trying to upgrade across version gaps that are not supported (eg: two minor versions), refresh the charm back to a supported version
* if something unexpected happens, use the [istioctl](https://istio.io/latest/docs/reference/commands/istioctl/) and debugging guidance from [Istio](https://istio.io/latest/) to diagnose and resolve any issues.  If you can restore the cluster to a running state that is at most one minor version behind your target version, you can then `juju resolved istio-pilot` to get Istio to retry the upgrade. 

If at any point you use the [istioctl](https://istio.io/latest/docs/reference/commands/istioctl/) tool to manually purge Istio from your cluster, it is recommended that you also remove the `istio-gateway` charm and redeploy it as needed.  

#### Upgrades across more than one minor version

When the charm detects an upgrade across more than one minor version, it will set an `ErrorStatus` and log the reason.  For example, if you have istio-pilot 1.13.0 deployed and you `juju refresh istio-pilot --channel 1.15/stable`, the upgrade-charm event will fail and the charm will go to `ErrorStatus`.  To resolve this error:
* refresh the charm again back to a supported version (`juju refresh istio-pilot --channel 1.14/stable`)
* if this refresh does not clear the error state immediately, use `juju resolve istio-pilot/0 --no-retry` to clear the error on the previous refresh event and move on to the next event.  This should lead to the refresh event for 1.14 firing, but it is a race so there could be other events that fire first

Once the new refresh event fires, the charm should recover and successfully upgrade Istio to 1.14.  You can then do a second refresh to get to Istio 1.15.    

If for some reason you cannot use the charm's typical upgrade procedure to get to within one minor version of your target version, you can use the [istioctl](https://istio.io/latest/docs/reference/commands/istioctl/) tool to manually upgrade through some versions and then `juju resolved istio-pilot/0` (without the `--no-retry`) to rerun the refresh event.  

#### Downgrades

If you accidentally attempt a downgrade, the istio-pilot charm will go to error state.  To resolve, `juju refresh` istio-pilot to a new charm version that is at or above the installed version of istio.

#### Unknown version errors

If the upgrade fails saying it cannot find the control plane version, this likely means your existing istio deployment is missing key pieces.  Use the [istioctl](https://istio.io/latest/docs/reference/commands/istioctl/) tool to inspect further. 

[^1]: Removal of the istio-ingressgateway application prior to upgrading istio-pilot is required because some versions of istio-pilot will hang indefinitely if istio-ingressgateway's workload pod is present in the cluster.  To remove the istio-ingressgateway application, use `juju remove-application istio-ingressgateway`.  Confirm that istio-ingressgateway is completely removed before proceeding by checking that the istio-ingressgateway application is no longer in `juju status` and the `istio-ingressgateway-workload` deployment is no longer in `kubectl get deployment -n kubeflow`.  If Juju hangs on removing the application, use `juju remove-application istio-ingressgateway --force` to force the removal.  Note that forcing the removal may leave the deployment in kubernetes, so be sure to check for that after forcing and delete the deployment manually if necessary.  
