# Infrastructure as Code

## Context

- **Consult Infrastructure as Code tiering and component contracts**: [`src/infra/docs/operator.md`](src/infra/docs/operator.md) — 3-tier architecture (components, topologies, deployments), protocol-first component interface contract, and strict handoff boundary to Argo CD.

## Principles

- **Synchronize IaC documentation**: keep `src/infra/docs/operator.md` synchronized whenever modifying or extending cloud foundations.
- **Enforce one-way parameter passing flow from deployments to components**: cloud foundation parameters flow strictly downstream: `deployments/` bind concrete cloud environments, accounts, apex domains, and nameserver delegations into input variables for reusable topologies and components; reusable modules in `src/infra/terraform/components` and `src/infra/terraform/topologies` must remain unbranded, generic, and cloud/region-flexible with zero default tenant domains or hardcoded cloud nameservers (such as AWS Route 53 `ns-*.awsdns-*` hostnames).
- **Project secrets, identities, and cloud storage downstream to Kubernetes**: OpenTofu provisions IAM/Pod Identity roles, KMS keys, cloud DNS zones, and external secret stores, passing identifiers and credentials into Kubernetes via cluster registration annotations and External Secrets Operator stores; in-cluster workloads and Argo CD components never create cloud credentials or hardcode static secrets.

## Decisions

- **Prefix every cloud object name with its owning cluster name**: name each cloud object `<cluster>-<purpose>[-<qualifier>]` using lowercase letters, digits, and single hyphens, with no repeated adjacent words, in at most 48 characters; the purpose names the workload in at most three plain words without repeating its parent system; Terraform map keys that become name segments use the same hyphenated form; names that a cloud provider or a controller generates keep their generated form.
- **Alias every encryption key**: give each KMS key an alias `alias/<kms_alias_prefix><cluster>-<purpose>` so no key is identified only by its generated ID.
- **Assign installation-wide cloud objects to the control plane cluster**: an object shared by every cluster of a deployment, such as operator credentials or global storage buckets, takes the control plane cluster name as its prefix, never a deployment, tenant, or installation word.
- **Suffix names in cross-account namespaces with the cloud account ID**: when name uniqueness spans more than one cloud account, as for S3 and R2 buckets, end the name with the owning cloud account ID, as in `cell-aws-usw2-home-400920695547`.
- **Name container repositories after their source path**: name container image repositories by the repository path of the source they are built from, as in `src/infra/tools/coder_snapshot_portal`; they belong to the deployment's registry rather than to a cluster, so they carry no cluster prefix.
- **Isolate deployments by cloud account and prefix only cluster names**: give each deployment its own cloud account, DNS domain, and tailnet; a deployment that must share an account sets `name_prefix`, which is prepended to its cluster names and to nothing else, and defaults to empty.
- **Apply customer compliance prefixes to IAM objects and key aliases**: a deployment in an account that constrains names sets `iam_name_prefix`, prepended verbatim before the cluster name of every IAM role, policy, user, and instance profile, `kms_alias_prefix`, prepended the same way to every KMS alias, and `iam_permissions_boundary`, attached to every IAM role; each prefix is at most 16 characters, all three default to empty, and none applies to other object types.
- **Tag every cloud object from one deployment tag map**: each deployment declares a single `tags` map with lowercase kebab-case keys, applies it through the cloud provider's default tags, and passes the same map through cluster registration annotations to every controller that creates cloud resources at runtime, such as node provisioners, load balancer controllers, and storage drivers.
