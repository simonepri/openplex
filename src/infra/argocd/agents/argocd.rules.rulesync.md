# GitOps

## Context

- **Consult GitOps architecture and sync wave documentation**: [`src/infra/docs/operator.md`](src/infra/docs/operator.md) — 2-tier ApplicationSet hierarchy, flat component conventions, atomic component boundaries, and fleet sync wave contract.

## Principles

- **Synchronize GitOps architecture documentation**: keep `src/infra/docs/operator.md` synchronized whenever modifying or extending GitOps manifests.
- **Keep Argo CD components cloud-, region-, tenant-, and profile-agnostic**: base manifests in `src/infra/argocd/components` represent pure, reusable Kubernetes workloads; they must never contain hardcoded cloud providers, regions, tenant domains (e.g. `openplex.*`), emails, or environment profile assumptions (`local` vs `production`). Use empty values (`""` or `[]`) or generic schema defaults in base manifests so unconfigured components fail fast, and inject deployment values dynamically via ApplicationSet patches from cluster registration annotations.
- **Bridge Terraform parameters to components via `ctrl.yaml` and `cells.yaml`**: OpenTofu outputs cluster annotations (domains, IAM identities, storage endpoints, backup buckets, admin emails) onto the Argo CD cluster registration records; the fleet ApplicationSets in `src/infra/argocd/apps/ctrl.yaml` (for control planes) and `src/infra/argocd/apps/cells.yaml` (for workload cells) read these annotations and dynamically inject them into components via Helm value overrides and Kustomize JSON6902 patches. Never configure child components directly out of band.
- **Configure workload scaling and sizing strictly in cluster availability profiles**: base components must not hardcode environment-specific replica counts, high-availability topologies, CPU/memory resource allocations, or PodDisruptionBudgets; configure all scaling and sizing overrides within `src/infra/argocd/apps/profiles.yaml` and ApplicationSet overlays so workloads adapt cleanly between local (`minimal`) and production (`resilient`) fleet profiles.
- **Consume secrets and IAM identities strictly downstream from Terraform**: Kubernetes manifests and component values must never declare static secrets or cloud credentials directly; consume OpenTofu-provisioned IAM roles via EKS Pod Identity associations and secret material via External Secrets Operator stores.
