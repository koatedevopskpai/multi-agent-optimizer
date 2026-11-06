output "instance_id" {
  value       = aws_instance.app.id
  description = "Single reference instance; stop or terminate through the reviewed lifecycle."
}

output "private_ip" {
  value       = aws_instance.app.private_ip
  description = "Private address; the application is reached only through SSM port forwarding."
}

output "ecr_repository_url" {
  value       = aws_ecr_repository.app.repository_url
  description = "Repository for the pinned digest image."
}

output "log_group_name" {
  value       = aws_cloudwatch_log_group.app.name
  description = "Correlated application log group."
}

output "data_volume_id" {
  value       = aws_ebs_volume.data.id
  description = "Encrypted persistent application/checkpoint volume."
}

output "ssm_port_forward_command" {
  value       = "aws ssm start-session --target ${aws_instance.app.id} --document-name AWS-StartPortForwardingSession --parameters '{\"portNumber\":[\"8000\"],\"localPortNumber\":[\"8000\"]}'"
  description = "Loopback-only operator access through SSM. This is transport, not application authentication."
}

output "deployed_image_uri" {
  value       = var.image_uri
  description = "Exact digest expected on the instance."
}
