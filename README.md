# Charmed istio

The `istio` service mesh is a dedicated infrastructure layer that you can add to your
applications. It allows you to transparently add capabilities like observability,
traffic management, and security, without adding them to your own code.
For more details of what is possible with istio, see https://istio.io/

## This bundle

This Juju charm bundle incorporates both

 - The [istio pilot charm](https://charmhub.io/istio-pilot)
 - The [istio ingressgateway charm](https://charmhub.io/istio-ingressgateway)

## Example Deployment

Deploy example bookinfo microservice application on microk8s:

    sudo snap install microk8s --classic
    microk8s.enable dns storage metallb:10.64.140.43-10.64.140.49 rbac
    microk8s.kubectl label namespace default istio-injection=enabled
    sleep 10
    juju bootstrap microk8s uk8s
    juju add-model istio-system microk8s
    juju deploy cs:~kubeflow-charmers/bundle/istio --channel edge
    sleep 30
    microk8s.kubectl patch role -n istio-system istio-ingressgateway-operator -p '{"apiVersion":"rbac.authorization.k8s.io/v1","kind":"Role","metadata":{"name":"istio-ingressgateway-operator"},"rules":[{"apiGroups":["*"],"resources":["*"],"verbs":["*"]}]}'
    microk8s.kubectl apply -f https://raw.githubusercontent.com/istio/istio/release-1.5/samples/bookinfo/platform/kube/bookinfo.yaml
    microk8s.kubectl apply -f https://raw.githubusercontent.com/istio/istio/release-1.5/samples/bookinfo/networking/bookinfo-gateway.yaml
    microk8s.kubectl wait --for=condition=ready pod --all --timeout=-1s

Congratulations, now `curl http://10.64.140.43/productpage`
