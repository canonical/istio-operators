output "app_name" {
  value = juju_application.istio_pilot.name
}

output "provides" {
  value = {
    metrics_endpoint  = "metrics-endpoint",
    grafana_dashboard = "grafana-dashboard",
    istio_pilot       = "istio-pilot",
    ingress           = "ingress",
    ingress_auth      = "ingress-auth",
    gateway_info      = "gatway-info"
  }
}

output "requires" {
  value = {
    certificates = "certificates"
  }
}
