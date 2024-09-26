output "app_name" {
  value = juju_application.istio_gateway.name
}

output "provides" {
  value = {
    metrics_endpoint = "metrics-endpoint"
  }
}

output "requires" {
  value = {
    istio_pilot = "istio-pilot"
  }
}
