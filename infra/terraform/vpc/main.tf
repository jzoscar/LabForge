# Placeholder module. LabForge passes: name, cidr, subnets (+ any `vars` from the IaC).
# Replace the body with your cloud provider's VPC resources.
variable "name" { type = string }
variable "cidr" { type = string }
variable "subnets" { type = list(any) }
variable "region" {
  type    = string
  default = ""
}

output "vpc_name" { value = var.name }
output "cidr" { value = var.cidr }
