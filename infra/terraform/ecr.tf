# ECR repositories, one per service image.
#
# Repository names are "<project_name>/<service>", e.g.
# "sentinel-sre-demo/citizen-service". The project prefix keeps this
# environment's images visibly separate from anything else in the account's
# registry.

resource "aws_ecr_repository" "services" {
  for_each = toset(var.ecr_repository_names)

  name                 = "${var.project_name}/${each.value}"
  image_tag_mutability = var.ecr_image_tag_mutability

  image_scanning_configuration {
    # Basic scanning on push. Free, and it means a known-vulnerable base
    # image shows up in the console without needing Inspector enabled
    # account-wide (which is a per-resource charge).
    scan_on_push = true
  }

  # Deliberately NOT set: force_delete. Without it, `terraform destroy`
  # refuses to remove a repository that still contains images, which is a
  # useful accident-guard — deleting images is how you lose the ability to
  # roll back.
  tags = {
    Name    = "${var.project_name}/${each.value}"
    Service = each.value
  }
}

# Expire untagged images only.
#
# Untagged images are the layers left behind when a tag is repointed (every
# rebuild of `latest` orphans the previous one). Nothing can reference them,
# so they have no rollback value — they are pure storage cost.
#
# Tagged images are never expired by this policy, on purpose: the SHA tags
# ARE the rollback targets. An automatic "keep only the last N tagged
# images" rule is exactly the kind of thing that deletes the image you
# needed thirty seconds before you needed it.
resource "aws_ecr_lifecycle_policy" "expire_untagged" {
  for_each = aws_ecr_repository.services

  repository = each.value.name

  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Expire untagged images after ${var.ecr_untagged_image_expiry_days} days"
      selection = {
        tagStatus   = "untagged"
        countType   = "sinceImagePushed"
        countUnit   = "days"
        countNumber = var.ecr_untagged_image_expiry_days
      }
      action = {
        type = "expire"
      }
    }]
  })
}
