"""Abstract base class for SoftEdIBO activities.

Activities are the plug-in unit of behaviour. Each concrete activity:

- Declares the robot type it works with (``robot_type``).
- Declares the user-tunable parameters it exposes (``PARAMS``) so the GUI
  can auto-generate a preset editor (see ``ActivityPreset`` in
  ``src/data/models.py`` and ``docs/ACTIVITIES.md``).
- Implements the lifecycle hooks ``_setup`` / ``start`` / ``pause`` /
  ``resume`` / ``stop`` and exposes its current state via ``get_state``.

Any activity can run in **simulation mode** — set ``simulation_mode = True``
on the instance (or via the SessionPanel checkbox) before ``setup`` is
called and the default ``prepare_robots`` will substitute real robots with
``SimulatedRobot`` instances backed by ``SimulatedController``. This
replaces the older standalone ``SimulationActivity``: any activity now has
a "test-without-hardware" mode for free.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar

from src.robots.base_robot import BaseRobot

if TYPE_CHECKING:
    from src.core.session import Session


# ---------------------------------------------------------------------------
# Param descriptor
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Param:
    """Single tunable parameter of an activity.

    The GUI uses these descriptors to auto-generate a preset editor form:

    - ``type == "int"`` / ``"float"`` → spin box (uses ``min`` / ``max``)
    - ``type == "bool"`` → checkbox
    - ``type == "color"`` → colour picker (default is ``"#RRGGBB"``)
    - ``type == "enum"`` → combo box (``choices`` lists allowed values)
    - ``type == "json"`` → multi-line text edit with JSON validation
    - ``type == "str"`` → single-line text edit
    """
    name: str
    type: str
    default: Any
    min: float | None = None
    max: float | None = None
    choices: tuple[Any, ...] = field(default_factory=tuple)
    label: str = ""              # display label; defaults to ``name`` if empty
    description: str = ""        # tooltip / help text

    def display_label(self) -> str:
        return self.label or self.name.replace("_", " ").capitalize()


# ---------------------------------------------------------------------------
# BaseActivity
# ---------------------------------------------------------------------------

class BaseActivity(ABC):
    """Abstract base class for all study activities."""

    robot_type: ClassVar[type[BaseRobot]]

    # Simulation-only knobs — apply when ``simulation_mode`` is on. Subclasses
    # inherit these automatically (merged with their own ``PARAMS``) so every
    # activity exposes the same sim controls in the GUI preset editor.
    SIM_PARAMS: ClassVar[tuple[Param, ...]] = (
        Param(
            name="sim_inflate_speed_pct_per_s",
            type="int", default=33, min=1, max=300,
            label="Sim inflate speed (%/s)",
            description="Simulated chamber fill rate. Higher = chambers reach "
                        "target faster in simulation. No effect on hardware.",
        ),
        Param(
            name="sim_deflate_speed_pct_per_s",
            type="int", default=33, min=1, max=300,
            label="Sim deflate speed (%/s)",
            description="Simulated chamber drain rate. Higher = chambers empty "
                        "faster in simulation. No effect on hardware.",
        ),
        Param(
            name="sim_touch_release_delay_ms",
            type="int", default=300, min=0, max=5000,
            label="Sim touch-release delay (ms)",
            description="After a simulated touch releases, wait this long "
                        "before the chamber starts deflating.",
        ),
    )

    # Activity-specific tunable params — subclasses set this to a tuple of
    # ``Param`` describing their knobs. The GUI auto-generates the editor.
    PARAMS: ClassVar[tuple[Param, ...]] = ()

    # When True, ``prepare_robots`` substitutes real robots with SimulatedRobot
    # counterparts. Set per-instance before ``setup`` (e.g. from the
    # SessionPanel "Simulation mode" checkbox).
    simulation_mode: bool = False

    @classmethod
    def all_params(cls) -> tuple[Param, ...]:
        """SIM_PARAMS first, then activity-specific PARAMS. Used by the GUI."""
        return cls.SIM_PARAMS + cls.PARAMS

    # Appended to the activity name for display / persistence when running in
    # simulation, so the operator can tell sim sessions apart in the UI and the
    # sessions table. ``get_activity`` strips it back off when resolving names.
    SIM_SUFFIX: ClassVar[str] = " (Simulation)"

    def __init__(self, name: str, description: str):
        self.name = name
        self.description = description
        # Live values for ALL declared params (SIM_PARAMS + PARAMS), populated
        # from defaults and overridden by ``apply_preset``.
        self.param_values: dict[str, Any] = {
            p.name: p.default for p in self.all_params()
        }

    @property
    def display_name(self) -> str:
        """Name shown in the UI / stored on the session record — tagged with
        the simulation suffix when running without hardware."""
        return f"{self.name}{self.SIM_SUFFIX}" if self.simulation_mode else self.name

    # ------------------------------------------------------------------
    # Preset handling
    # ------------------------------------------------------------------

    def apply_preset(self, params: dict[str, Any]) -> None:
        """Overlay preset values onto ``self.param_values``.

        Unknown keys are ignored (forward-compat with old presets after a
        param is renamed/removed); missing keys keep their PARAMS default.
        """
        valid_keys = {p.name for p in self.all_params()}
        for key, value in (params or {}).items():
            if key in valid_keys:
                self.param_values[key] = value

    def current_preset(self) -> dict[str, Any]:
        """Return a copy of the live values (suitable for saving as a preset)."""
        return dict(self.param_values)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def prepare_robots(self, robots: list[BaseRobot]) -> list[BaseRobot]:
        """Wrap the input robots when ``simulation_mode`` is on, else pass
        through unchanged. Override to add activity-specific wrapping."""
        if self.simulation_mode:
            from src.activities._simulation import wrap_robots_in_simulation
            return wrap_robots_in_simulation(robots, self.param_values)
        return robots

    def setup(self, session: "Session", robots: list[BaseRobot]) -> None:
        """Validate robot types and delegate to :meth:`_setup`.

        Raises:
            TypeError: If any robot is not an instance of :attr:`robot_type`
                — unless ``simulation_mode`` is on, in which case the robots
                have already been swapped for ``SimulatedRobot`` (which is a
                ``BaseRobot``, not the activity's nominal ``robot_type``).
        """
        if not self.simulation_mode:
            wrong = [r for r in robots if not isinstance(r, self.robot_type)]
            if wrong:
                raise TypeError(
                    f"{type(self).__name__} requires {self.robot_type.__name__} "
                    f"robots, got: {[type(r).__name__ for r in wrong]}"
                )
        self._setup(session, robots)

    @abstractmethod
    def _setup(self, session: "Session", robots: list[BaseRobot]) -> None:
        """Activity-specific setup logic. Called after robot type validation."""
        ...

    @abstractmethod
    def start(self) -> None:
        """Start the activity."""
        ...

    def pause(self) -> None:
        """Pause the activity. Default: no-op."""

    def resume(self) -> None:
        """Resume the activity after a pause. Default: no-op."""

    @abstractmethod
    def stop(self) -> None:
        """Stop the activity."""
        ...

    @abstractmethod
    def get_state(self) -> dict[str, Any]:
        """Return the current activity state as a dictionary."""
        ...
