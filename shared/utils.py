"""
shared/utils.py
===============
Sharding utilities for the distributed Game of Life.

Grid layout (300 × 300):
  ┌──────────┬──────────┬──────────┐
  │  zone-1  │  zone-2  │  zone-3  │
  │  x: 0-99 │ x:100-199│ x:200-299│
  └──────────┴──────────┴──────────┘
  y always spans 0-299 for every zone.

Each Worker reads GRID_ZONE from its environment and calls
`get_zone_config()` to know exactly which columns it owns,
plus which Redis hosts neighbour it (needed for ghost-cell
exchange at zone boundaries).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

# ──────────────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────────────

GRID_WIDTH: int = 60
GRID_HEIGHT: int = 50
ZONE_WIDTH: int = GRID_WIDTH // 3   # 20 columns per zone

# Maps a zone name to its Redis host env-var and column boundaries.
# Boundaries are INCLUSIVE on both ends.
ZONE_DEFINITIONS: dict[str, dict] = {
    "zone-1": {"x_start": 0,  "x_end": 19, "redis_host_env": "REDIS_1_HOST"},
    "zone-2": {"x_start": 20, "x_end": 39, "redis_host_env": "REDIS_2_HOST"},
    "zone-3": {"x_start": 40, "x_end": 59, "redis_host_env": "REDIS_3_HOST"},
}

# Ordered list used to resolve left/right neighbours.
ZONE_ORDER: list[str] = ["zone-1", "zone-2", "zone-3"]


# ──────────────────────────────────────────────────────────────────────────────
# Data classes
# ──────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class NeighbourZone:
    """Describes an adjacent zone that shares a vertical boundary."""
    zone_name: str
    redis_host: str
    redis_port: int
    # Which column in *this* zone is the boundary column facing the neighbour.
    # e.g. for zone-1's right neighbour: our boundary_col = 99,
    # their ghost_col = 100.
    our_boundary_col: int
    their_ghost_col: int


@dataclass
class ZoneConfig:
    """Full configuration a Worker needs to process its shard."""
    zone_name: str

    # Column range this worker owns (inclusive).
    x_start: int
    x_end: int

    # Row range (same for all zones).
    y_start: int = 0
    y_end: int = GRID_HEIGHT - 1

    # Redis connection for this zone's own data.
    redis_host: str = "localhost"
    redis_port: int = 6379

    # Adjacent zones whose edge cells we must read as ghost cells.
    left_neighbour: NeighbourZone | None = None
    right_neighbour: NeighbourZone | None = None

    # ── Derived helpers ──────────────────────────────────────────────────────

    @property
    def width(self) -> int:
        return self.x_end - self.x_start + 1

    @property
    def height(self) -> int:
        return self.y_end - self.y_start + 1

    @property
    def total_cells(self) -> int:
        return self.width * self.height

    def owns_cell(self, x: int, y: int) -> bool:
        """Return True if cell (x, y) belongs to this zone."""
        return (self.x_start <= x <= self.x_end) and (self.y_start <= y <= self.y_end)

    def iter_cells(self):
        """Yield every (x, y) coordinate owned by this zone."""
        for x in range(self.x_start, self.x_end + 1):
            for y in range(self.y_start, self.y_end + 1):
                yield x, y

    def is_left_boundary(self, x: int) -> bool:
        """True if column x is the leftmost column of this zone."""
        return x == self.x_start

    def is_right_boundary(self, x: int) -> bool:
        """True if column x is the rightmost column of this zone."""
        return x == self.x_end


# ──────────────────────────────────────────────────────────────────────────────
# Core sharding functions
# ──────────────────────────────────────────────────────────────────────────────

def get_zone_for_x(x: int) -> str:
    """
    Return which zone owns column *x*.

    >>> get_zone_for_x(0)
    'zone-1'
    >>> get_zone_for_x(99)
    'zone-1'
    >>> get_zone_for_x(100)
    'zone-2'
    >>> get_zone_for_x(250)
    'zone-3'
    """
    if not (0 <= x < GRID_WIDTH):
        raise ValueError(f"x={x} is outside the grid (0–{GRID_WIDTH - 1})")

    for zone_name, defn in ZONE_DEFINITIONS.items():
        if defn["x_start"] <= x <= defn["x_end"]:
            return zone_name

    # Should never reach here given the guard above.
    raise RuntimeError(f"No zone found for x={x}")


def get_zone_for_cell(x: int, y: int) -> str:
    """
    Return which zone owns cell (x, y).

    Sharding is column-based only; y is validated but does not affect routing.
    """
    if not (0 <= y < GRID_HEIGHT):
        raise ValueError(f"y={y} is outside the grid (0–{GRID_HEIGHT - 1})")
    return get_zone_for_x(x)


def _resolve_redis_host(host_env_var: str, fallback: str = "localhost") -> str:
    """Read a Redis hostname from an environment variable."""
    return os.environ.get(host_env_var, fallback)


def _build_neighbour(
    zone_name: str,
    our_boundary_col: int,
    their_ghost_col: int,
    redis_port: int,
) -> NeighbourZone:
    host_env = ZONE_DEFINITIONS[zone_name]["redis_host_env"]
    return NeighbourZone(
        zone_name=zone_name,
        redis_host=_resolve_redis_host(host_env, fallback=zone_name),
        redis_port=redis_port,
        our_boundary_col=our_boundary_col,
        their_ghost_col=their_ghost_col,
    )


def get_zone_config(
    zone_name: str | None = None,
    redis_port: int | None = None,
) -> ZoneConfig:
    """
    Build the full ZoneConfig for a worker.

    Parameters
    ----------
    zone_name:
        Override for the zone name. Defaults to the ``GRID_ZONE`` env var.
    redis_port:
        Override for the Redis port. Defaults to the ``REDIS_PORT`` env var
        (fallback: 6379).

    Raises
    ------
    ValueError
        If the resolved zone name is not one of the configured zones.

    Examples
    --------
    >>> import os
    >>> os.environ["GRID_ZONE"] = "zone-2"
    >>> os.environ["REDIS_PORT"] = "6379"
    >>> cfg = get_zone_config()
    >>> cfg.x_start, cfg.x_end
    (100, 199)
    >>> cfg.left_neighbour.zone_name
    'zone-1'
    >>> cfg.right_neighbour.zone_name
    'zone-3'
    """
    # ── Resolve parameters ───────────────────────────────────────────────────
    zone_name = zone_name or os.environ.get("GRID_ZONE", "zone-1")
    redis_port = redis_port or int(os.environ.get("REDIS_PORT", 6379))

    if zone_name not in ZONE_DEFINITIONS:
        raise ValueError(
            f"Unknown zone '{zone_name}'. "
            f"Valid zones: {list(ZONE_DEFINITIONS.keys())}"
        )

    defn = ZONE_DEFINITIONS[zone_name]
    x_start: int = defn["x_start"]
    x_end: int = defn["x_end"]

    # ── Own Redis host ───────────────────────────────────────────────────────
    # In a Worker container REDIS_HOST is injected by docker-compose.
    # Outside Docker (tests, local dev) fall back to the zone name which
    # matches the docker-compose service name.
    redis_host = os.environ.get("REDIS_HOST", _resolve_redis_host(defn["redis_host_env"], zone_name))

    # ── Neighbours ───────────────────────────────────────────────────────────
    zone_idx = ZONE_ORDER.index(zone_name)

    left_neighbour: NeighbourZone | None = None
    right_neighbour: NeighbourZone | None = None

    if zone_idx > 0:
        left_name = ZONE_ORDER[zone_idx - 1]
        left_neighbour = _build_neighbour(
            zone_name=left_name,
            our_boundary_col=x_start,          # our leftmost column
            their_ghost_col=x_start - 1,       # their rightmost column (ghost)
            redis_port=redis_port,
        )

    if zone_idx < len(ZONE_ORDER) - 1:
        right_name = ZONE_ORDER[zone_idx + 1]
        right_neighbour = _build_neighbour(
            zone_name=right_name,
            our_boundary_col=x_end,            # our rightmost column
            their_ghost_col=x_end + 1,         # their leftmost column (ghost)
            redis_port=redis_port,
        )

    return ZoneConfig(
        zone_name=zone_name,
        x_start=x_start,
        x_end=x_end,
        redis_host=redis_host,
        redis_port=redis_port,
        left_neighbour=left_neighbour,
        right_neighbour=right_neighbour,
    )


# ──────────────────────────────────────────────────────────────────────────────
# Redis key helpers
# ──────────────────────────────────────────────────────────────────────────────

def cell_key(x: int, y: int) -> str:
    """Canonical Redis key for a single cell's state (0 = dead, 1 = alive)."""
    return f"cell:{x}:{y}"


def ghost_col_key(x: int) -> str:
    """
    Redis key used to publish an entire ghost column as a packed string.

    Format: ``ghost:col:<x>``  →  value: 300-char string of '0'/'1'.
    """
    return f"ghost:col:{x}"


def generation_key(generation: int) -> str:
    """Key used to signal that a generation is complete."""
    return f"generation:{generation}:done"


# ──────────────────────────────────────────────────────────────────────────────
# Quick self-test (python shared/utils.py)
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import json

    print("=" * 60)
    print("  Game of Life – Sharding Self-Test")
    print("=" * 60)

    # 1. Zone routing
    test_cases = [
        (0, "zone-1"), (99, "zone-1"),
        (100, "zone-2"), (199, "zone-2"),
        (200, "zone-3"), (299, "zone-3"),
    ]
    print("\n[1] get_zone_for_x()")
    for x, expected in test_cases:
        result = get_zone_for_x(x)
        status = "✓" if result == expected else "✗"
        print(f"  {status}  x={x:>3}  →  {result}")

    # 2. ZoneConfig for each zone
    print("\n[2] get_zone_config() per zone")
    for zone in ZONE_ORDER:
        os.environ["GRID_ZONE"] = zone
        os.environ["REDIS_HOST"] = zone           # simulate docker-compose injection
        cfg = get_zone_config()
        left  = cfg.left_neighbour.zone_name  if cfg.left_neighbour  else "None"
        right = cfg.right_neighbour.zone_name if cfg.right_neighbour else "None"
        print(
            f"  {cfg.zone_name}: cols {cfg.x_start:>3}–{cfg.x_end:>3} "
            f"| {cfg.total_cells:,} cells "
            f"| ← {left:<8} → {right}"
        )

    # 3. Boundary detection for zone-2
    os.environ["GRID_ZONE"] = "zone-2"
    os.environ["REDIS_HOST"] = "zone-2"
    cfg2 = get_zone_config()
    print("\n[3] Boundary detection for zone-2")
    for x in [99, 100, 150, 199, 200]:
        owned   = cfg2.owns_cell(x, 0)
        is_left = cfg2.is_left_boundary(x)
        is_right= cfg2.is_right_boundary(x)
        print(f"  x={x:>3}  owns={owned!s:<5}  left_boundary={is_left!s:<5}  right_boundary={is_right!s:<5}")

    # 4. Redis key helpers
    print("\n[4] Key helpers")
    print(f"  cell_key(150, 75)    → {cell_key(150, 75)}")
    print(f"  ghost_col_key(100)   → {ghost_col_key(100)}")
    print(f"  generation_key(42)   → {generation_key(42)}")

    print("\n✓ All tests passed.\n")
