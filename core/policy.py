"""Policy Engine — enforces lifecycle transitions and promotion eligibility."""
from __future__ import annotations

import datetime
from pathlib import Path
from typing import Any

import yaml


class PolicyViolationError(Exception):
    """Raised when a memory operation violates the configured policy."""


VALID_STAGES = [
    "captured",
    "classified",
    "stored",
    "retrieved",
    "used",
    "validated",
    "promoted",
    "demoted",
    "archived",
    "forgotten",
]


class PolicyEngine:
    """Reads wiki/policy.yaml and enforces memory lifecycle rules."""

    def __init__(self, policy_path: str) -> None:
        self._policy_path = policy_path
        self._config: dict[str, Any] = {}
        self._load()

    def _load(self) -> None:
        path = Path(self._policy_path)
        if not path.exists():
            raise FileNotFoundError(f"Policy file not found: {self._policy_path}")
        with path.open() as f:
            self._config = yaml.safe_load(f) or {}

    def _layers(self) -> dict[str, Any]:
        return self._config.get("layers", {})

    def validate_capture(self, layer: str, item: dict[str, Any]) -> None:
        """Raise PolicyViolationError if the target layer is unknown."""
        if layer not in self._layers():
            raise PolicyViolationError(
                f"Unknown layer '{layer}'. Valid layers: {list(self._layers().keys())}"
            )

    def validate_promote(self, item: dict[str, Any], target_layer: str) -> None:
        """
        Raise PolicyViolationError if item does not meet promotion requirements.

        Checks:
        - target_layer must be the next layer for the item's current layer
        - age_hours >= threshold
        - access_count >= threshold
        - quality_score >= threshold
        """
        current_layer = item.get("layer")
        layers = self._layers()

        if current_layer not in layers:
            raise PolicyViolationError(f"Unknown current layer '{current_layer}'.")

        expected_next = layers[current_layer].get("promotes_to")
        if expected_next != target_layer:
            raise PolicyViolationError(
                f"Layer '{current_layer}' promotes to '{expected_next}', "
                f"not '{target_layer}'."
            )

        thresholds = layers[current_layer].get("promotion", {})

        # Age check
        required_age = thresholds.get("age_hours", 0)
        created_at_str = item.get("created_at", "")
        if created_at_str:
            created_at_str_clean = created_at_str.rstrip("Z")
            created_at = datetime.datetime.fromisoformat(created_at_str_clean).replace(
                tzinfo=datetime.timezone.utc
            )
            now = datetime.datetime.now(datetime.timezone.utc)
            actual_age_hours = (now - created_at).total_seconds() / 3600.0
            if actual_age_hours < required_age:
                raise PolicyViolationError(
                    f"Item age {actual_age_hours:.4f}h is below required age "
                    f"{required_age}h for promotion from '{current_layer}'."
                )

        # Access count check
        required_access = thresholds.get("access_count", 0)
        actual_access = item.get("access_count", 0)
        if actual_access < required_access:
            raise PolicyViolationError(
                f"access_count {actual_access} is below required {required_access} "
                f"for promotion from '{current_layer}'."
            )

        # Quality score check
        required_quality = thresholds.get("quality_score", 0.0)
        actual_quality = item.get("quality_score", 0.0)
        if actual_quality < required_quality:
            raise PolicyViolationError(
                f"quality_score {actual_quality} is below required {required_quality} "
                f"for promotion from '{current_layer}'."
            )

    def validate_demote(self, item: dict[str, Any], target_layer: str) -> None:
        """
        Raise PolicyViolationError if demotion is not permitted.

        Checks:
        - target_layer must be a known layer
        - target_layer must differ from the item's current layer
        """
        layers = self._layers()

        if target_layer not in layers:
            raise PolicyViolationError(
                f"Unknown target layer '{target_layer}'. Valid layers: {list(layers.keys())}"
            )

        current_layer = item.get("layer")
        if target_layer == current_layer:
            raise PolicyViolationError(
                f"Cannot demote item to its current layer '{current_layer}'."
            )

        # If the policy config specifies an explicit demotes_to field, honour it.
        demotes_to = layers.get(current_layer, {}).get("demotes_to")
        if demotes_to is not None and target_layer != demotes_to:
            raise PolicyViolationError(
                f"Layer '{current_layer}' demotes to '{demotes_to}', "
                f"not '{target_layer}'."
            )

    def validate_forget(self, item: dict[str, Any]) -> None:
        """Raise PolicyViolationError if item is not in 'archived' stage."""
        forget_cfg = self._config.get("forget", {})
        requires_archived = forget_cfg.get("requires_archived", True)
        if requires_archived and item.get("stage") != "archived":
            raise PolicyViolationError(
                f"Item must be archived before it can be forgotten. "
                f"Current stage: '{item.get('stage')}'."
            )

    def check_lifecycle_stage(self, item: dict[str, Any], required_stage: str) -> None:
        """Raise PolicyViolationError if item is not in the required stage."""
        actual_stage = item.get("stage")
        if actual_stage != required_stage:
            raise PolicyViolationError(
                f"Expected item stage '{required_stage}' but got '{actual_stage}'."
            )

    def check_promotion_eligible(self, item: dict) -> bool:
        """Return True if the item meets all promotion thresholds, False otherwise.

        Unlike validate_promote(), this method never raises — it is designed for
        silent side-effect checks inside gateway operations.
        """
        current_layer = item.get("layer", "")
        layers = self._layers()

        if current_layer not in layers:
            return False

        next_layer = layers[current_layer].get("promotes_to")
        if not next_layer:
            # Already at top layer — no promotion possible
            return False

        thresholds = layers[current_layer].get("promotion", {})

        # Age check
        required_age = thresholds.get("age_hours", 0)
        created_at_str = item.get("created_at", "")
        if created_at_str and required_age > 0:
            created_at_str_clean = created_at_str.rstrip("Z")
            try:
                created_at = datetime.datetime.fromisoformat(created_at_str_clean).replace(
                    tzinfo=datetime.timezone.utc
                )
                now = datetime.datetime.now(datetime.timezone.utc)
                actual_age_hours = (now - created_at).total_seconds() / 3600.0
                if actual_age_hours < required_age:
                    return False
            except ValueError:
                return False

        # Access count check
        required_access = thresholds.get("access_count", 0)
        actual_access = item.get("access_count", 0)
        if actual_access < required_access:
            return False

        # Quality score check
        required_quality = thresholds.get("quality_score", 0.0)
        actual_quality = item.get("quality_score", 0.0)
        if actual_quality < required_quality:
            return False

        return True

    def get_next_layer(self, current_layer: str) -> str | None:
        """Return the next layer in the promotion chain, or None if at top."""
        layers = self._layers()
        if current_layer not in layers:
            raise PolicyViolationError(f"Unknown layer '{current_layer}'.")
        return layers[current_layer].get("promotes_to")

    def get_layer_path_template(self, layer: str) -> str:
        """Return the path template for the given layer."""
        layers = self._layers()
        if layer not in layers:
            raise PolicyViolationError(f"Unknown layer '{layer}'.")
        return layers[layer].get("path_template", "")
