variable "cluster_name" {
  type        = string
  default     = "shl"
  description = "Kind cluster name (kube context becomes kind-<name>)"
}

variable "app_node_port" {
  type        = number
  default     = 30080
  description = "Host port mapped to the app Service NodePort"
}
