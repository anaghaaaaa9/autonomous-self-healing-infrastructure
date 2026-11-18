"""Optional executor: run remediation through Ansible playbooks (executor: ansible)."""
from __future__ import annotations

import json
import os
from typing import Tuple

from .common import run

PLAYBOOK_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "ansible", "playbooks")

PLAYBOOKS = {
    "start_instance": ("container_recover.yml", {"container_action": "start"}),
    "restart_instance": ("container_recover.yml", {"container_action": "restart"}),
    "delete_pod": ("kube_delete_pod.yml", {}),
    "restart_deployment": ("kube_restart_deployment.yml", {}),
    "rollout_undo": ("kube_rollout_undo.yml", {}),
    "scale_out": ("kube_scale_deployment.yml", {}),
    "scale_in": ("kube_scale_deployment.yml", {}),
}


def run_playbook(action: str, extra_vars: dict, runner=run) -> Tuple[bool, str]:
    if action not in PLAYBOOKS:
        return False, f"no playbook for action {action}"
    name, fixed = PLAYBOOKS[action]
    path = os.path.abspath(os.path.join(PLAYBOOK_DIR, name))
    ev = {**fixed, **extra_vars}
    rc, out = runner(["ansible-playbook", "-i", "localhost,", "-c", "local", path,
                      "-e", json.dumps(ev)], 120)
    return rc == 0, f"ansible:{name} rc={rc} {out[-200:]}"
