resource "juju_application" "istio_pilot" {
  charm {
    name     = "istio-pilot"
    base     = var.base
    base     = var.base
    channel  = var.channel
    revision = var.revision
  }
  config    = var.config
  model     = var.model_name
  name      = var.app_name
  resources = var.resources
  trust     = true
  units     = 1
}
