# Provisions EC2 Tailscale subnet router instances, security groups, KMS keys, and Secrets Manager tokens.

module "interface" {
  source = "../_interface"

  name              = var.name
  vpc_id            = var.vpc_id
  subnet_id         = var.subnet_id
  tailnet_auth_key  = var.tailnet_auth_key
  advertised_routes = var.advertised_routes
  realized = {
    instance_id = aws_instance.this.id
    private_ip  = aws_instance.this.private_ip
  }
}

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
    description = "Allow STUN UDP"
    from_port   = 3478
    to_port     = 3478
    protocol    = "udp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    description = "Allow Tailscale WireGuard and DERP UDP"
    from_port   = 41641
    to_port     = 41641
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
  name = "${module.interface.names.instance}-router"

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
    Name = "${module.interface.names.instance}-router"
  }
}

resource "aws_iam_role_policy" "router" {
  name = "${module.interface.names.instance}-router-secret"
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
  name = "${module.interface.names.instance}-router"
  role = aws_iam_role.router.name
}

resource "aws_instance" "this" {
  ami                         = data.aws_ami.al2023.id
  instance_type               = "t4g.nano"
  subnet_id                   = var.subnet_id
  vpc_security_group_ids      = [aws_security_group.this.id]
  iam_instance_profile        = aws_iam_instance_profile.router.name
  source_dest_check           = false
  user_data_replace_on_change = true

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 2
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

    dnf install -y yum-utils awscli
    dnf config-manager --add-repo https://pkgs.tailscale.com/stable/amazon-linux/2023/tailscale.repo
    dnf install -y tailscale

    systemctl enable --now tailscaled

    ROUTES_FLAG=""
    if [ -n "${join(",", var.advertised_routes)}" ]; then
      ROUTES_FLAG="--advertise-routes=${join(",", var.advertised_routes)}"
    fi

    AUTHKEY=$(aws secretsmanager get-secret-value --secret-id "${aws_secretsmanager_secret.tailscale_auth_key.arn}" --query SecretString --output text)

    tailscale up --authkey="$AUTHKEY" --hostname="${module.interface.names.instance}" $ROUTES_FLAG --accept-routes
  EOF

  tags = {
    Name = module.interface.names.instance
  }
}
