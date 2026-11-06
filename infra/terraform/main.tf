resource "aws_security_group" "app" {
  name_prefix = "${var.project_tag}-"
  description = "Loopback-only application; outbound SSM, ECR and logs"
  vpc_id      = data.aws_vpc.selected.id

  egress {
    description = "HTTPS for SSM, ECR and CloudWatch Logs"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    description = "DNS through the VPC resolver"
    from_port   = 53
    to_port     = 53
    protocol    = "udp"
    cidr_blocks = ["169.254.169.253/32"]
  }

  egress {
    description = "DNS over TCP fallback"
    from_port   = 53
    to_port     = 53
    protocol    = "tcp"
    cidr_blocks = ["169.254.169.253/32"]
  }

  tags = merge(local.tags, { Name = "${var.project_tag}-app" })
}

resource "terraform_data" "account_guard" {
  input = data.aws_caller_identity.current.account_id

  lifecycle {
    precondition {
      condition = (
        data.aws_caller_identity.current.account_id == var.expected_account_id
        && can(regex(":assumed-role/${var.expected_stage_role_name}/", data.aws_caller_identity.current.arn))
      )
      error_message = "Resolved account/role does not match the reviewed stage identity; refusing to continue."
    }
  }
}

resource "aws_iam_policy" "runtime_boundary" {
  name        = "${var.project_tag}-runtime-boundary"
  description = "Maximum permissions for the single reference runtime role"
  policy      = var.runtime_boundary_policy_json
  tags        = local.tags
}

resource "aws_iam_role" "instance" {
  name                 = "${var.project_tag}-runtime"
  description          = "Runtime role: SSM, ECR pull and CloudWatch Logs only"
  permissions_boundary = aws_iam_policy.runtime_boundary.arn
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
  tags = local.tags
}

resource "aws_iam_role_policy_attachment" "ssm" {
  role       = aws_iam_role.instance.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_role_policy" "ecr_pull" {
  name = "${var.project_tag}-ecr-pull"
  role = aws_iam_role.instance.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = "ecr:GetAuthorizationToken"
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = [
          "ecr:BatchCheckLayerAvailability",
          "ecr:BatchGetImage",
          "ecr:GetDownloadUrlForLayer"
        ]
        Resource = "arn:aws:ecr:${var.aws_region}:${var.expected_account_id}:repository/${var.ecr_repository_name}"
      }
    ]
  })
}

resource "aws_iam_role_policy" "logs" {
  name = "${var.project_tag}-logs"
  role = aws_iam_role.instance.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "logs:CreateLogStream",
        "logs:DescribeLogStreams",
        "logs:PutLogEvents"
      ]
      Resource = "arn:aws:logs:${var.aws_region}:${var.expected_account_id}:log-group:${var.log_group_name}:*"
    }]
  })
}

resource "aws_iam_instance_profile" "app" {
  name = "${var.project_tag}-runtime"
  role = aws_iam_role.instance.name
  tags = local.tags
}

resource "aws_ecr_repository" "app" {
  name                 = var.ecr_repository_name
  image_tag_mutability = "IMMUTABLE"
  force_delete         = true

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "AES256"
  }

  tags = local.tags
}

resource "aws_ecr_lifecycle_policy" "app" {
  repository = aws_ecr_repository.app.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Expire untagged images after one day"
      selection = {
        tagStatus   = "untagged"
        countType   = "sinceImagePushed"
        countUnit   = "days"
        countNumber = 1
      }
      action = { type = "expire" }
    }]
  })
}

resource "aws_cloudwatch_log_group" "app" {
  name              = var.log_group_name
  retention_in_days = var.log_retention_days
  tags              = local.tags
}

resource "aws_ebs_volume" "data" {
  availability_zone = data.aws_subnet.selected.availability_zone
  size              = var.data_volume_size_gb
  type              = "gp3"
  encrypted         = true
  tags              = merge(local.tags, { Name = "${var.project_tag}-data" })
}

resource "aws_volume_attachment" "data" {
  device_name = var.data_device_name
  volume_id   = aws_ebs_volume.data.id
  instance_id = aws_instance.app.id
}

resource "aws_instance" "app" {
  ami                         = data.aws_ami.amazon_linux_2023.id
  instance_type               = var.instance_type
  subnet_id                   = local.selected_subnet
  vpc_security_group_ids      = [aws_security_group.app.id]
  iam_instance_profile        = aws_iam_instance_profile.app.name
  associate_public_ip_address = true
  monitoring                  = false
  ebs_optimized               = true
  user_data_replace_on_change = false

  credit_specification {
    cpu_credits = "standard"
  }

  root_block_device {
    volume_type           = "gp3"
    volume_size           = var.root_volume_size_gb
    encrypted             = true
    delete_on_termination = true
  }

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }

  user_data = templatefile("${path.module}/templates/user-data.sh.tftpl", {
    aws_region            = var.aws_region
    image_uri             = var.image_uri
    log_group_name        = aws_cloudwatch_log_group.app.name
    project_tag           = var.project_tag
    volume_id             = aws_ebs_volume.data.id
    bootstrap_archive_b64 = filebase64(data.archive_file.bootstrap.output_path)
  })

  tags = merge(local.tags, { Name = "${var.project_tag}-app" })
}
