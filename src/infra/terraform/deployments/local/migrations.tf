# Declares state migration blocks reconciling local emulator Docker Compose resources with OpenTofu state.

removed {
  from = module.floci_runtime

  lifecycle {
    destroy = false
  }
}
