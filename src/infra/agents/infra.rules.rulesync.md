# Infrastructure Architecture & Documentation Index

## Context

- **OpenTofu**: owns cloud foundations and the Argo CD bootstrap.
- **Argo CD**: owns everything in-cluster.
- **Reference documentation tracks**: [`src/infra/docs/developer.md`](src/infra/docs/developer.md) for the developer experience, and [`src/infra/docs/operator.md`](src/infra/docs/operator.md) for the fleet operations lifecycle.

## Principles

- **Synchronize documentation guides**: keep `src/infra/docs/developer.md` and `src/infra/docs/operator.md` synchronized whenever modifying developer workflows or infrastructure components.
- **Avoid overloaded terms and verify context meanings**: avoid umbrella terms like 'platform', 'system', or 'service' without explicit qualifiers, as they obscure boundaries; check whether terms have conflicting or established technical meanings elsewhere in the repository before using them, and name the exact layer, mechanism, or actor instead (such as the fleet, control plane, worker cell, cloud foundations, storage engine, or admission controller).

## Decisions

- **Provision local fleet before running chainsaw tests**: chainsaw suites need `mise run //src/infra:up` (or `mise run up` within `src/infra`) first; `mise run //src/infra:chainsaw` runs them against the local fleet.
