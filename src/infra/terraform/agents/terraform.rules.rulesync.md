# Infrastructure as Code

## Context

- **Consult Infrastructure as Code tiering and component contracts**: [`src/infra/docs/operator.md`](src/infra/docs/operator.md) — 3-tier architecture (components, topologies, deployments), protocol-first component interface contract, and strict handoff boundary to Argo CD.

## Principles

- **Synchronize IaC documentation**: keep `src/infra/docs/operator.md` synchronized whenever modifying or extending cloud foundations.
- **Enforce one-way parameter passing flow from deployments to components**: cloud foundation parameters flow strictly downstream: `deployments/` bind concrete cloud environments, accounts, apex domains, and nameserver delegations into input variables for reusable topologies and components; reusable modules in `src/infra/terraform/components` and `src/infra/terraform/topologies` must remain unbranded, generic, and cloud/region-flexible with zero default tenant domains or hardcoded cloud nameservers (such as AWS Route 53 `ns-*.awsdns-*` hostnames).
- **Project secrets, identities, and cloud storage downstream to Kubernetes**: OpenTofu provisions IAM/Pod Identity roles, KMS keys, cloud DNS zones, and external secret stores, passing identifiers and credentials into Kubernetes via cluster registration annotations and External Secrets Operator stores; in-cluster workloads and Argo CD components never create cloud credentials or hardcode static secrets.
