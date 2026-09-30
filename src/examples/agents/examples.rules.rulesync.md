# Workload Projects

## Principles

- **Colocate workload project assets and manifests together**: each directory containing `deployment/project.yaml` is a stable project identity; keep a workload project's source, Bazel targets, OCI image definitions, `.k8s.yaml` runtime resources, and colocated checks together.
- **Place workload Kubernetes manifests under deployment directory**: all Kubernetes manifests live under `deployment/`, where `kustomization.yaml` is the discovery marker; the same manifest template serves both continuous reconciliation and on-demand submission.
- **Colocate system capability tests with capability definitions**: system capability conformance belongs with the corresponding capability, never under a synthetic team.
- **Store tabular datasets as Parquet and stream via Arrow IPC**: materialize large tabular datasets as Parquet in object storage; use Arrow IPC only for transient high-speed exchange.
- **Govern team quotas and identities through team contracts**: team identity and Kueue quota are team-owned system contracts.
