# Tests node repair configuration and node monitoring agent add-on in the AWS EKS cluster component.

mock_provider "aws" {
  mock_resource "aws_iam_role" {
    defaults = {
      arn = "arn:aws:iam::123456789012:role/mock"
    }
  }

  mock_resource "aws_kms_key" {
    defaults = {
      arn = "arn:aws:kms:us-east-1:123456789012:key/mock"
    }
  }

  mock_resource "aws_cloudwatch_log_group" {
    defaults = {
      arn = "arn:aws:logs:us-east-1:123456789012:log-group:mock"
    }
  }

  mock_resource "aws_launch_template" {
    defaults = {
      id             = "lt-1234567890abcdef0"
      latest_version = 1
    }
  }

  mock_resource "aws_eks_cluster" {
    defaults = {
      endpoint = "https://mock.eks.us-east-1.amazonaws.com"
      certificate_authority = [{
        data = "dGVzdC1jYQ=="
      }]
      identity = [{
        oidc = [{
          issuer = "https://oidc.eks.us-east-1.amazonaws.com/id/MOCK"
        }]
      }]
      vpc_config = {
        cluster_security_group_id = "sg-12345678"
      }
    }
  }

  mock_resource "aws_sqs_queue" {
    defaults = {
      arn = "arn:aws:sqs:us-east-1:123456789012:test-cluster-karpenter"
      id  = "https://sqs.us-east-1.amazonaws.com/123456789012/test-cluster-karpenter"
    }
  }

  mock_data "aws_caller_identity" {
    defaults = {
      account_id = "123456789012"
      arn        = "arn:aws:iam::123456789012:root"
    }
  }

  mock_data "aws_region" {
    defaults = {
      name = "us-east-1"
    }
  }

  mock_data "aws_vpc" {
    defaults = {
      cidr_block = "10.0.0.0/16"
    }
  }
}

variables {
  cluster_name = "test-cluster"
  vpc_id       = "vpc-12345678"
  subnet_ids   = ["subnet-11111111", "subnet-22222222"]
}

run "verifies_node_repair_and_monitoring_agent" {
  command = plan

  assert {
    condition     = aws_eks_addon.node_monitoring_agent[0].addon_name == "eks-node-monitoring-agent"
    error_message = "Node monitoring agent add-on must be installed."
  }

  assert {
    condition     = jsondecode(aws_eks_addon.node_monitoring_agent[0].configuration_values).nodeAgent.tolerations == [{ operator = "Exists" }]
    error_message = "Node monitoring agent must tolerate every taint so it runs on GPU and system nodes."
  }

  assert {
    condition     = aws_eks_addon.metrics_server[0].addon_name == "metrics-server"
    error_message = "Metrics server add-on must be installed."
  }

  assert {
    condition     = aws_eks_node_group.system.node_repair_config[0].enabled == true
    error_message = "System node group must have node repair enabled."
  }

  assert {
    condition     = length(aws_eks_node_group.system.taint) == 0
    error_message = "System node group taints must default to empty."
  }
}

run "verifies_node_repair_can_be_disabled" {
  command = plan

  variables {
    node_repair_enabled = false
  }

  assert {
    condition     = length(aws_eks_node_group.system.node_repair_config) == 0
    error_message = "System node group must allow disabling node repair."
  }
}

run "routes_pods_to_dedicated_subnets_with_prefix_delegation" {
  command = plan

  variables {
    pod_subnet_ids = ["subnet-pod0000a"]
  }

  override_data {
    target = data.aws_subnet.pod
    values = { availability_zone = "us-east-1a" }
  }

  assert {
    condition     = jsondecode(aws_eks_addon.vpc_cni[0].configuration_values).env.AWS_VPC_K8S_CNI_CUSTOM_NETWORK_CFG == "true"
    error_message = "Pod subnets must enable VPC CNI custom networking."
  }

  assert {
    condition     = jsondecode(aws_eks_addon.vpc_cni[0].configuration_values).env.ENABLE_PREFIX_DELEGATION == "true"
    error_message = "Pod subnets must enable prefix delegation."
  }

  assert {
    condition = jsondecode(aws_eks_addon.vpc_cni[0].configuration_values).eniConfig.subnets == {
      "us-east-1a" = { id = "subnet-pod0000a", securityGroups = ["sg-12345678"] }
    }
    error_message = "The pod subnet must be keyed by its availability zone with the cluster security group."
  }

  assert {
    condition     = strcontains(base64decode(aws_launch_template.system.user_data), "maxPods: 110")
    error_message = "System nodes must raise kubelet maxPods when prefix delegation is enabled."
  }
}

run "keeps_pods_on_node_subnets_without_pod_subnets" {
  command = plan

  assert {
    condition     = !can(jsondecode(aws_eks_addon.vpc_cni[0].configuration_values).env)
    error_message = "Without pod subnets the VPC CNI must keep its default networking."
  }

  assert {
    condition     = aws_launch_template.system.user_data == null
    error_message = "Without pod subnets system nodes must keep the default maxPods."
  }
}

run "verifies_system_node_taints" {
  command = plan

  variables {
    system_node_taints = [
      {
        key    = "CriticalAddonsOnly"
        value  = "true"
        effect = "NO_SCHEDULE"
      }
    ]
  }

  assert {
    condition     = length(aws_eks_node_group.system.taint) == 1
    error_message = "System node group must have the configured taint."
  }

  assert {
    condition     = tolist(aws_eks_node_group.system.taint)[0].key == "CriticalAddonsOnly" && tolist(aws_eks_node_group.system.taint)[0].value == "true" && tolist(aws_eks_node_group.system.taint)[0].effect == "NO_SCHEDULE"
    error_message = "Taint key, value, and effect must match."
  }
}

run "verifies_karpenter_interruption_resources" {
  command = plan

  assert {
    condition     = length(aws_sqs_queue.karpenter_interruption) == 1
    error_message = "Karpenter interruption queue must be planned when enabled."
  }

  assert {
    condition     = aws_sqs_queue.karpenter_interruption[0].name == "test-cluster-karpenter"
    error_message = "Karpenter interruption queue must be named after the cluster."
  }

  assert {
    condition     = aws_sqs_queue.karpenter_interruption[0].message_retention_seconds == 300
    error_message = "Karpenter interruption queue retention must default to 300 seconds."
  }

  assert {
    condition     = aws_sqs_queue.karpenter_interruption[0].sqs_managed_sse_enabled == true
    error_message = "Karpenter interruption queue must enable SSE."
  }

  assert {
    condition     = aws_cloudwatch_event_target.karpenter_spot_interruption[0].arn == aws_sqs_queue.karpenter_interruption[0].arn
    error_message = "Spot interruption target must route to the Karpenter interruption queue."
  }

  assert {
    condition     = aws_cloudwatch_event_target.karpenter_rebalance[0].arn == aws_sqs_queue.karpenter_interruption[0].arn
    error_message = "Rebalance target must route to the Karpenter interruption queue."
  }

  assert {
    condition     = aws_cloudwatch_event_target.karpenter_instance_state_change[0].arn == aws_sqs_queue.karpenter_interruption[0].arn
    error_message = "Instance state change target must route to the Karpenter interruption queue."
  }

  assert {
    condition     = aws_cloudwatch_event_target.karpenter_health_event[0].arn == aws_sqs_queue.karpenter_interruption[0].arn
    error_message = "Health event target must route to the Karpenter interruption queue."
  }

  assert {
    condition     = output.karpenter_interruption_queue_arn == aws_sqs_queue.karpenter_interruption[0].arn
    error_message = "Queue ARN output must match Karpenter interruption queue."
  }

  assert {
    condition     = output.karpenter_interruption_queue_name == "test-cluster-karpenter"
    error_message = "Queue name output must match Karpenter interruption queue."
  }
}

run "verifies_karpenter_interruption_disabled" {
  command = plan

  variables {
    enable_karpenter_interruption = false
  }

  assert {
    condition     = length(aws_sqs_queue.karpenter_interruption) == 0
    error_message = "Karpenter interruption queue must not be created when disabled."
  }

  assert {
    condition     = length(aws_cloudwatch_event_rule.karpenter_spot_interruption) == 0
    error_message = "Karpenter spot interruption rule must not be created when disabled."
  }

  assert {
    condition     = output.karpenter_interruption_queue_arn == null
    error_message = "Queue ARN output must be null when Karpenter interruption is disabled."
  }

  assert {
    condition     = output.karpenter_interruption_queue_name == null
    error_message = "Queue name output must be null when Karpenter interruption is disabled."
  }
}
