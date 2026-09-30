# Node Problem Detector (`node-problem-detector`)

Deploys the upstream Kubernetes `node-problem-detector` agent as a portable daemonset to monitor whole-node host, kernel, device, and filesystem health across all platform cells.

## Overview

Provider-specific node conditions are typically emitted only on managed cloud worker groups (such as AWS or GCP-specific agents). Floci bare-metal and non-EKS/GKE cells receive none by default. Furthermore, several critical system failure modes previously went undetected or unmitigated:

- **GPU Xid Errors:** GPU driver or PCIe failures requiring node drain and reset.
- **Filesystem Read-Only Remounts:** Root or ephemeral disk dropping to read-only mode after storage I/O errors.
- **Mount Hangs:** Unresponsive CSI or rclone storage mounts causing blocked workload processes.
- **Service Restart Loops:** Kubelet or containerd crash loops.

## Monitors Configured

1. **Kernel Monitor (`kernel-monitor.json`):**
   - Monitors `/dev/kmsg` for kernel hung tasks and deadlocks (`KernelDeadlock`).
   - Detects read-only filesystem remounts, ext4, and XFS I/O corruption (`ReadonlyFilesystem`).
2. **GPU Xid Monitor (`gpu-xid-monitor.json`):**
   - Monitors kernel logs for critical NVIDIA driver Xid errors (`GPUProblem`):
     - **Xid 48:** Double-Bit ECC Error (DBE) - uncorrectable DRAM memory corruption.
     - **Xid 54:** Auxiliary power disconnect - GPU power delivery interruption.
     - **Xid 62:** Internal microcontroller halt - GPU management core failure.
     - **Xid 64:** Page retirement / row remapping failure - hardware fault containment failure.
     - **Xid 74:** Fatal NVLink Error - NVLink fabric communication loss.
     - **Xid 79:** GPU fallen off the bus - PCIe link-down event.
     - **Xid 92:** High single-bit ECC error rate - impending uncorrectable failure.
     - **Xid 95:** Robust Channel Uncontained Error - system-wide uncontainable memory error.
     - **Xid 119:** GSP RPC timeout - GPU system processor firmware unresponsive.
     - **Xid 120:** GSP firmware halt - GPU system processor firmware crashed.
3. **Mount Health Custom Plugin (`mount-health-monitor.json` & `check-mount-health.sh`):**
   - Probes CSI, rclone (`fuse.rclone`), and network filesystem mounts with strict 5-second timeout probes to detect hung mounts and transport disconnections (`MountHung`).
4. **Systemd Monitor (`systemd-monitor.json`):**
   - Tracks crash loops and repeated restarts of host `kubelet.service` (`FrequentKubeletRestart`) and `containerd.service` (`FrequentContainerdRestart`).

## GKE Rationale & Condition Alignment

On Google Kubernetes Engine (GKE), Google provides a built-in Node Problem Detector addon managed by the GKE control plane. The built-in GKE addon manages conditions including `KernelDeadlock`, `ReadonlyFilesystem`, `FrequentKubeletRestart`, and `FrequentContainerdRestart`.

To ensure consistent behavior and prevent conflicts:

- **Condition Naming Parity:** The conditions configured here strictly adopt the standard names (`KernelDeadlock`, `ReadonlyFilesystem`, `FrequentKubeletRestart`, `FrequentContainerdRestart`) matching GKE.
- **Deduplication / Skip Policy:** On GKE clusters where the cloud-managed NPD addon is active, this component is omitted from deployment via ApplicationSet cluster target selectors to prevent duplicate reporting and status race conditions. Non-GKE cells (AWS EKS, bare-metal Floci) deploy this component to achieve full operational parity.

## Automated Repair & Remediation

A companion `Cleaner` custom resource (`node-problem-repair`) evaluates nodes reporting persistent problem conditions:

- **Threshold:** Triggers only when a problem condition has remained `True` for at least 300 seconds (5 minutes).
- **Dry-Run by Default:** Configured with `action: Scan` (dry-run detection only) to identify degradation safely without disruptive action.
- **Remediation:** When active (`action: Transform`), taints the degraded node with `node.kubernetes.io/unhealthy=problem-detected:NoSchedule` and signals Karpenter for `NodeClaim` disruption.
