import unittest

from forge.core.errors import InvalidStateTransitionError
from forge.core.models import DeploymentStatus, validate_transition


class TestStateMachine(unittest.TestCase):
    def test_valid_happy_path_transitions(self) -> None:
        sequence = [
            (DeploymentStatus.PENDING, DeploymentStatus.BUILDING),
            (DeploymentStatus.BUILDING, DeploymentStatus.STARTING),
            (DeploymentStatus.STARTING, DeploymentStatus.HEALTH_CHECKING),
            (DeploymentStatus.HEALTH_CHECKING, DeploymentStatus.ACTIVE),
            (DeploymentStatus.ACTIVE, DeploymentStatus.STOPPING),
            (DeploymentStatus.STOPPING, DeploymentStatus.STOPPED),
        ]
        for current, target in sequence:
            validate_transition(current, target)

    def test_same_status_transition_is_noop(self) -> None:
        validate_transition(DeploymentStatus.BUILDING, DeploymentStatus.BUILDING)
        validate_transition(DeploymentStatus.ACTIVE, DeploymentStatus.ACTIVE)

    def test_failure_transitions_from_intermediate_states(self) -> None:
        fail_sources = [
            DeploymentStatus.PENDING,
            DeploymentStatus.BUILDING,
            DeploymentStatus.STARTING,
            DeploymentStatus.HEALTH_CHECKING,
            DeploymentStatus.ACTIVE,
            DeploymentStatus.STOPPING,
        ]
        for status in fail_sources:
            validate_transition(status, DeploymentStatus.FAILED)

    def test_rollback_transition_from_active(self) -> None:
        validate_transition(DeploymentStatus.ACTIVE, DeploymentStatus.ROLLED_BACK)

    def test_invalid_skip_state_transitions_raise_error(self) -> None:
        invalid_pairs = [
            (DeploymentStatus.PENDING, DeploymentStatus.ACTIVE),
            (DeploymentStatus.PENDING, DeploymentStatus.STARTING),
            (DeploymentStatus.BUILDING, DeploymentStatus.ACTIVE),
            (DeploymentStatus.HEALTH_CHECKING, DeploymentStatus.STOPPED),
        ]
        for current, target in invalid_pairs:
            with self.assertRaises(InvalidStateTransitionError):
                validate_transition(current, target)

    def test_invalid_backwards_and_terminal_transitions(self) -> None:
        invalid_pairs = [
            (DeploymentStatus.ACTIVE, DeploymentStatus.BUILDING),
            (DeploymentStatus.ACTIVE, DeploymentStatus.STARTING),
            (DeploymentStatus.STOPPED, DeploymentStatus.ACTIVE),
            (DeploymentStatus.STOPPED, DeploymentStatus.BUILDING),
            (DeploymentStatus.FAILED, DeploymentStatus.PENDING),
            (DeploymentStatus.FAILED, DeploymentStatus.ACTIVE),
            (DeploymentStatus.ROLLED_BACK, DeploymentStatus.ACTIVE),
        ]
        for current, target in invalid_pairs:
            with self.assertRaises(InvalidStateTransitionError):
                validate_transition(current, target)


if __name__ == "__main__":
    unittest.main()
