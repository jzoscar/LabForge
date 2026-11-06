# Placeholder module. LabForge passes: name, vip, listeners, targets (map host -> address).
variable "name" { type = string }
variable "vip" {
  type    = string
  default = null
}
variable "listeners" { type = list(any) }
variable "targets" { type = map(string) }

output "lb_name" { value = var.name }
