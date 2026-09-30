# Exports Argo CD bootstrap state including release details and application registration status.

output "record" {
  description = "Record of the Argo CD bootstrap installation."
  value = {
    installed              = true
    root_application       = "fleet-root"
    namespace              = local.namespace
    cluster_name           = var.cluster_name
    cluster_endpoint       = var.cluster_endpoint
    cluster_ca_certificate = var.cluster_ca_certificate
    fleet_availability     = var.fleet_availability
  }
}
