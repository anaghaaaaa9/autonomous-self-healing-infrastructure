terraform {
  required_version = ">= 1.5"
  required_providers {
    kind = { source = "tehcyx/kind", version = "~> 0.5" }
    null = { source = "hashicorp/null", version = "~> 3.2" }
  }
}

# 1. Kubernetes-in-Docker cluster (single node, NodePort 30080 published to the host)
resource "kind_cluster" "shl" {
  name           = var.cluster_name
  wait_for_ready = true

  kind_config {
    kind        = "Cluster"
    api_version = "kind.x-k8s.io/v1alpha4"

    node {
      role = "control-plane"
      extra_port_mappings {
        container_port = 30080
        host_port      = var.app_node_port
      }
    }
  }
}

# 2. Build/load the app image and apply the manifests (namespace, RBAC, deployment, service)
resource "null_resource" "workload" {
  triggers = {
    cluster  = kind_cluster.shl.id
    manifest = sha1(join("", [for f in fileset("${path.module}/../kubernetes", "**/*.yaml") : filesha1("${path.module}/../kubernetes/${f}")]))
    app      = filesha1("${path.module}/../application/app.py")
  }

  provisioner "local-exec" {
    working_dir = "${path.module}/.."
    command     = <<-EOT
      docker build -t shl-app:latest . &&
      kind load docker-image shl-app:latest --name ${var.cluster_name} &&
      kubectl --context kind-${var.cluster_name} apply -f kubernetes/namespace.yaml -f kubernetes/rbac -f kubernetes/deployments -f kubernetes/services &&
      kubectl --context kind-${var.cluster_name} -n shl rollout status deployment/shl-app --timeout=120s
    EOT
  }
}

output "kube_context" {
  value = "kind-${var.cluster_name}"
}

output "app_url" {
  value = "http://localhost:${var.app_node_port}"
}
