output "public_ip" {
  description = "Public IPv4 address of the K3s node. This is the citizen portal's entrypoint."
  value       = aws_instance.k3s.public_ip
}

output "portal_url" {
  description = "URL to open in a browser once the application is deployed."
  value       = "http://${aws_instance.k3s.public_ip}"
}

output "instance_id" {
  description = "EC2 instance id, needed for SSM commands."
  value       = aws_instance.k3s.id
}

output "ssm_session_command" {
  description = "Copy-paste command to open an administrative shell on the node. No SSH key, no open SSH port."
  value       = "aws ssm start-session --target ${aws_instance.k3s.id} --region ${var.aws_region}"
}

output "ecr_registry" {
  description = "ECR registry hostname for `docker login`."
  value       = local.ecr_registry
}

output "ecr_repository_urls" {
  description = "Full push/pull URL for each service repository, keyed by service name."
  value       = { for name, repo in aws_ecr_repository.services : name => repo.repository_url }
}

output "github_actions_role_arn" {
  description = <<-EOT
    ARN to set as the AWS_ROLE_TO_ASSUME GitHub secret. Null when
    var.github_repository was not set (no CI role created).
  EOT
  value       = local.create_github_role ? aws_iam_role.github_actions[0].arn : null
}

output "aws_region" {
  description = "Region everything was created in. Set this as the AWS_REGION GitHub variable."
  value       = var.aws_region
}

output "vpc_id" {
  description = "Id of the VPC created for this environment."
  value       = aws_vpc.main.id
}

output "next_steps" {
  description = "What to do after apply. Deliberately not claiming the environment is ready — it is not, until the application is deployed and verified."
  value       = <<-EOT
    Terraform has created the infrastructure. The environment is NOT yet
    running the application. Next:

      1. Wait for bootstrap to finish (K3s install takes ~3-5 minutes):
           aws ssm start-session --target ${aws_instance.k3s.id} --region ${var.aws_region}
           sudo cat /var/lib/sentinel/bootstrap-complete
           sudo tail -50 /var/log/sentinel-bootstrap.log

      2. Confirm the cluster and Traefik are healthy:
           sudo kubectl get nodes
           sudo kubectl -n kube-system get pods

      3. Build and push images, then deploy. See docs/aws-deployment.md
         steps 16-26.

      4. Verify at http://${aws_instance.k3s.public_ip}

    Full step-by-step, including verification and the Sentinel autonomous
    remediation test, is in docs/aws-deployment.md.
  EOT
}
