# Provisions EC2 Tailscale subnet router instances, security groups, KMS keys, and Secrets Manager tokens.

module "interface" {
  source = "../_interface"

  name                = var.name
  vpc_id              = var.vpc_id
  subnet_id           = var.subnet_id
  tailnet_auth_key    = var.tailnet_auth_key
  advertised_routes   = var.advertised_routes
  enable_k8s_operator = var.enable_k8s_operator
  operator_tags       = var.operator_tags
  realized = {
    instance_id                  = aws_instance.this.id
    primary_network_interface_id = aws_instance.this.primary_network_interface_id
    private_ip                   = aws_instance.this.private_ip
  }
}

data "aws_region" "current" {}

data "aws_ami" "al2023" {
  most_recent = true
  owners      = ["amazon"]

  filter {
    name   = "name"
    values = ["al2023-ami-2023.*-arm64"]
  }

  filter {
    name   = "state"
    values = ["available"]
  }
}

locals {
  # VPC jumbo frames exceed the tunnel MTU, so forwarded TCP sessions must advertise a segment size that fits it.
  mss_clamp_commands = var.clamp_tunnel_mss ? join("\n", [
    "",
    "dnf install -y iptables-nft",
    "iptables -t mangle -A FORWARD -i tailscale0 -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --set-mss 1240",
    "iptables -t mangle -A FORWARD -o tailscale0 -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --clamp-mss-to-pmtu",
  ]) : ""
  masquerade_commands = var.masquerade_tunnel_egress ? join("\n", [
    "",
    "dnf install -y iptables-nft",
    "iptables -t nat -A POSTROUTING -o tailscale0 -j MASQUERADE",
  ]) : ""
}

resource "aws_security_group" "this" {
  name        = module.interface.names.instance
  description = "Security group for network mesh router ${module.interface.names.instance}"
  vpc_id      = var.vpc_id

  ingress {
    description = "Allow Tailscale WireGuard UDP"
    from_port   = 41641
    to_port     = 41641
    protocol    = "udp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    description = "Allow internal mesh traffic"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"]
  }

  egress {
    description = "Allow HTTPS outbound"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    description = "Allow Tailscale WireGuard and peer traffic across UDP"
    from_port   = 0
    to_port     = 65535
    protocol    = "udp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = module.interface.names.instance
  }
}

resource "aws_kms_key" "tailscale_auth_key" {
  description             = "Customer-managed key for network mesh router ${module.interface.names.instance} Tailscale auth key"
  deletion_window_in_days = 7
  enable_key_rotation     = true

  tags = {
    Name = "${module.interface.names.instance}-tailscale-auth-key"
  }
}

resource "aws_kms_alias" "tailscale_auth_key" {
  name          = "alias/${var.kms_alias_prefix}${var.cluster_name}-tailscale-auth-key"
  target_key_id = aws_kms_key.tailscale_auth_key.key_id
}

resource "aws_secretsmanager_secret" "tailscale_auth_key" {
  name                    = "${module.interface.names.instance}-tailscale-auth-key"
  recovery_window_in_days = 0
  kms_key_id              = aws_kms_key.tailscale_auth_key.arn

  tags = {
    Name = "${module.interface.names.instance}-tailscale-auth-key"
  }
}

resource "aws_secretsmanager_secret_version" "tailscale_auth_key" {
  secret_id     = aws_secretsmanager_secret.tailscale_auth_key.id
  secret_string = var.tailnet_auth_key
}

resource "aws_iam_role" "router" {
  name                 = "${var.iam_name_prefix}${module.interface.names.instance}"
  permissions_boundary = var.iam_permissions_boundary

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "ec2.amazonaws.com"
        }
      }
    ]
  })

  tags = {
    Name = "${var.iam_name_prefix}${module.interface.names.instance}"
  }
}

resource "aws_iam_role_policy" "router" {
  name = "${var.iam_name_prefix}${module.interface.names.instance}-secret"
  role = aws_iam_role.router.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "secretsmanager:GetSecretValue"
        ]
        Resource = aws_secretsmanager_secret.tailscale_auth_key.arn
      },
      {
        Effect = "Allow"
        Action = [
          "kms:Decrypt"
        ]
        Resource = aws_kms_key.tailscale_auth_key.arn
      }
    ]
  })
}

resource "aws_iam_instance_profile" "router" {
  name = "${var.iam_name_prefix}${module.interface.names.instance}"
  role = aws_iam_role.router.name
}

resource "aws_instance" "this" {
  ami                         = data.aws_ami.al2023.id
  instance_type               = var.instance_type
  subnet_id                   = var.subnet_id
  vpc_security_group_ids      = [aws_security_group.this.id]
  iam_instance_profile        = aws_iam_instance_profile.router.name
  source_dest_check           = false
  user_data_replace_on_change = true

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }

  root_block_device {
    delete_on_termination = true
    encrypted             = true
    volume_size           = 20
    volume_type           = "gp3"
  }

  user_data = <<-EOF
    #!/bin/bash
    set -euo pipefail

    echo 'net.ipv4.ip_forward = 1' > /etc/sysctl.d/99-tailscale.conf
    echo 'net.ipv6.conf.all.forwarding = 1' >> /etc/sysctl.d/99-tailscale.conf
    sysctl -p /etc/sysctl.d/99-tailscale.conf

    fallocate -l 1G /swapfile
    chmod 600 /swapfile
    mkswap /swapfile
    swapon /swapfile

    dnf install -y yum-utils awscli
    dnf config-manager --add-repo https://pkgs.tailscale.com/stable/amazon-linux/2023/tailscale.repo
    dnf install -y tailscale

    systemctl enable --now tailscaled

    ROUTES_FLAG=""
    if [ -n "${join(",", var.advertised_routes)}" ]; then
      ROUTES_FLAG="--advertise-routes=${join(",", var.advertised_routes)}"
    fi

    AUTHKEY=$(aws secretsmanager get-secret-value --region "${data.aws_region.current.region}" --secret-id "${aws_secretsmanager_secret.tailscale_auth_key.arn}" --query SecretString --output text)

    tailscale up --authkey="$AUTHKEY" --hostname="${module.interface.names.instance}" $ROUTES_FLAG --accept-routes${local.mss_clamp_commands}${local.masquerade_commands}
  EOF

  tags = {
    Name = module.interface.names.instance
  }
}

resource "tailscale_oauth_client" "k8s_operator" {
  count       = var.enable_k8s_operator ? 1 : 0
  description = "Tailscale operator OAuth client for ${var.name}"
  scopes      = ["devices:core", "auth_keys"]
  tags        = var.operator_tags
}

resource "aws_kms_key" "operator_oauth" {
  count                   = var.enable_k8s_operator ? 1 : 0
  description             = "Customer-managed key for network mesh router ${module.interface.names.instance} operator OAuth secret"
  deletion_window_in_days = 7
  enable_key_rotation     = true

  tags = {
    Name = module.interface.names.operator_oauth
  }
}

resource "aws_kms_alias" "operator_oauth" {
  count         = var.enable_k8s_operator ? 1 : 0
  name          = "alias/${var.kms_alias_prefix}${var.cluster_name}-operator-oauth"
  target_key_id = aws_kms_key.operator_oauth[0].key_id
}

resource "aws_secretsmanager_secret" "operator_oauth" {
  count                   = var.enable_k8s_operator ? 1 : 0
  name                    = module.interface.names.operator_oauth
  recovery_window_in_days = 0
  kms_key_id              = aws_kms_key.operator_oauth[0].arn

  tags = {
    Name = module.interface.names.operator_oauth
  }
}

resource "aws_secretsmanager_secret_version" "operator_oauth" {
  count     = var.enable_k8s_operator ? 1 : 0
  secret_id = aws_secretsmanager_secret.operator_oauth[0].id
  secret_string = jsonencode({
    client_id     = try(tailscale_oauth_client.k8s_operator[0].id, "")
    client_secret = try(tailscale_oauth_client.k8s_operator[0].key, "")
  })
}
