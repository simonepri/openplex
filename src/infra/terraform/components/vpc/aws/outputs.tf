# Exports VPC IDs, CIDR blocks, subnet IDs categorized by tier, NAT gateway IPs, and route table IDs.

output "record" {
  description = "Canonical output record for the VPC component."
  value = merge(module.interface.record, {
    private_route_table_ids = [for rt in aws_route_table.private : rt.id]
    pod_route_table_ids     = [for rt in aws_route_table.pod : rt.id]
  })
}

output "resolver_query_log_group_arn" {
  description = "ARN of the Route 53 Resolver query log group."
  value       = try(aws_cloudwatch_log_group.resolver_queries[0].arn, null)
}

output "resolver_query_log_group_name" {
  description = "Name of the Route 53 Resolver query log group."
  value       = try(aws_cloudwatch_log_group.resolver_queries[0].name, null)
}



