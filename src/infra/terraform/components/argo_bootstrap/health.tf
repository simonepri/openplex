# Requires current KubeRay readiness before Argo CD reports Ray workloads healthy.

locals {
  ray_health = {
    RayService = <<-LUA
      local health = {status = "Progressing", message = "Waiting for RayService readiness"}
      if obj.status == nil or obj.status.observedGeneration ~= obj.metadata.generation then
        return health
      end
      for _, condition in ipairs(obj.status.conditions or {}) do
        if condition.type == "Ready" then
          health.message = condition.message or health.message
          if condition.status == "True" and (condition.observedGeneration == nil or condition.observedGeneration == obj.metadata.generation) then
            health.status = "Healthy"
          end
          return health
        end
      end
      return health
    LUA
    RayCluster = <<-LUA
      local health = {status = "Progressing", message = "Waiting for RayCluster readiness"}
      if obj.status == nil or obj.status.observedGeneration ~= obj.metadata.generation then
        return health
      end
      local headReady = false
      local provisioned = false
      for _, condition in ipairs(obj.status.conditions or {}) do
        if condition.type == "HeadPodReady" then
          headReady = condition.status == "True"
          health.message = condition.message or health.message
          if condition.reason == "CrashLoopBackOff" then
            health.status = "Degraded"
            return health
          end
        elseif condition.type == "RayClusterProvisioned" then
          provisioned = condition.status == "True"
        end
      end
      if headReady and provisioned and (obj.status.readyWorkerReplicas or 0) >= (obj.status.desiredWorkerReplicas or 0) then
        health.status = "Healthy"
        health.message = "Ray head and requested workers are ready"
      end
      return health
    LUA
  }
}
