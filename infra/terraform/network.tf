# Networking: a dedicated VPC with exactly one public subnet.
#
# There is no NAT Gateway and no private subnet, and that is a cost
# decision, not an oversight. A NAT Gateway is ~$32/month before data
# processing charges — more than the EC2 instance itself in this
# environment. The single node needs outbound internet (to install K3s and
# pull from ECR), and the cheapest way to give a single instance outbound
# access is to put it in a public subnet with a public IP and route through
# the Internet Gateway, which costs nothing beyond the IP address itself.
#
# The security tradeoff that buys is handled at the security-group layer
# (see security.tf): the instance is publicly *addressable* but only ports
# 80/443 are reachable. No SSH, no Kubernetes API, no database ports.

locals {
  # Fall back to the region's first available AZ when the caller did not
  # pin one. data.aws_availability_zones only reads AWS's AZ list; it
  # creates nothing and touches nothing.
  availability_zone = coalesce(var.availability_zone, data.aws_availability_zones.available.names[0])
}

data "aws_availability_zones" "available" {
  state = "available"

  filter {
    name   = "opt-in-status"
    values = ["opt-in-not-required"]
  }
}

resource "aws_vpc" "main" {
  cidr_block = var.vpc_cidr

  # Both required for the instance to get an internal DNS name, which the
  # SSM agent and the ECR credential refresh timer both rely on.
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = {
    Name = "${var.project_name}-vpc"
  }
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id

  tags = {
    Name = "${var.project_name}-igw"
  }
}

resource "aws_subnet" "public" {
  vpc_id            = aws_vpc.main.id
  cidr_block        = var.public_subnet_cidr
  availability_zone = local.availability_zone

  # The instance needs a public IP to reach the internet through the IGW
  # (no NAT Gateway exists to do it for them).
  map_public_ip_on_launch = true

  tags = {
    Name = "${var.project_name}-public-subnet"
  }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }

  tags = {
    Name = "${var.project_name}-public-rt"
  }
}

resource "aws_route_table_association" "public" {
  subnet_id      = aws_subnet.public.id
  route_table_id = aws_route_table.public.id
}
