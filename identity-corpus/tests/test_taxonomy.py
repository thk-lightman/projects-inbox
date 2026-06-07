"""Sub-AC 3: taxonomy.yaml structural assertions.

Loads taxonomy.yaml and verifies:
  * Exactly 5 axes (I_structural_frames .. V_epistemic_register).
  * Each axis has a 3-level hierarchy: axis -> group -> leaf.
  * Total leaf count == 61.
  * Every leaf is addressable as (axis, group, leaf).
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
TAXONOMY_PATH = REPO_ROOT / "taxonomy.yaml"

EXPECTED_AXES = [
    "I_structural_frames",
    "II_narrative_tools",
    "III_functional_acts",
    "IV_interview_moves",
    "V_epistemic_register",
]
EXPECTED_TOTAL_LEAVES = 61


def _load_taxonomy() -> dict:
    with TAXONOMY_PATH.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def test_taxonomy_file_exists() -> None:
    assert TAXONOMY_PATH.is_file(), f"taxonomy.yaml missing at {TAXONOMY_PATH}"


def test_taxonomy_has_five_axes() -> None:
    data = _load_taxonomy()
    axes = data.get("axes")
    assert isinstance(axes, dict), "axes must be a mapping"
    assert list(axes.keys()) == EXPECTED_AXES, (
        f"axes mismatch: got {list(axes.keys())}, expected {EXPECTED_AXES}"
    )
    assert len(axes) == 5


def test_every_leaf_has_axis_group_leaf_path() -> None:
    data = _load_taxonomy()
    axes = data["axes"]
    triples: list[tuple[str, str, str]] = []
    for axis_key, axis_body in axes.items():
        assert isinstance(axis_body, dict), f"axis {axis_key} must be mapping"
        groups = axis_body.get("groups")
        assert isinstance(groups, dict) and groups, (
            f"axis {axis_key} must have non-empty groups"
        )
        for group_name, leaves in groups.items():
            assert isinstance(leaves, list) and leaves, (
                f"group {axis_key}/{group_name} must be non-empty list"
            )
            for leaf in leaves:
                assert isinstance(leaf, str) and leaf, (
                    f"leaf under {axis_key}/{group_name} must be non-empty string"
                )
                triples.append((axis_key, group_name, leaf))

    # No duplicate (axis, group, leaf) triples.
    assert len(triples) == len(set(triples)), "duplicate (axis,group,leaf) triple found"


def test_total_leaf_count_is_61() -> None:
    data = _load_taxonomy()
    axes = data["axes"]
    total = sum(
        len(leaves)
        for axis_body in axes.values()
        for leaves in axis_body["groups"].values()
    )
    assert total == EXPECTED_TOTAL_LEAVES, (
        f"expected {EXPECTED_TOTAL_LEAVES} leaves, got {total}"
    )


def test_axes_i_to_iii_carry_operator_rhetorical_taxonomy() -> None:
    """Sanity-check that axes I–III preserve the operator's pre-existing
    rhetorical taxonomy (counts derived from seed: 18 + 8 + 5 = 31)."""
    data = _load_taxonomy()
    axes = data["axes"]

    def leaf_count(axis_key: str) -> int:
        return sum(len(v) for v in axes[axis_key]["groups"].values())

    assert leaf_count("I_structural_frames") == 18
    assert leaf_count("II_narrative_tools") == 8
    assert leaf_count("III_functional_acts") == 5


def test_axes_iv_and_v_added_for_interview_self_transfer() -> None:
    """Axes IV (Interview & Presentation Moves) and V (Epistemic & Register)
    are the additions for English self-transfer; sum = 30 (IV gained the
    Audience_Engagement group: Rhetorical_Question/Direct_Address/Emphasis)."""
    data = _load_taxonomy()
    axes = data["axes"]

    def leaf_count(axis_key: str) -> int:
        return sum(len(v) for v in axes[axis_key]["groups"].values())

    assert leaf_count("IV_interview_moves") == 19
    assert leaf_count("V_epistemic_register") == 11


def test_axes_iv_v_keys_name_interview_moves_and_epistemic_register() -> None:
    """AC text: 'axes IV/V contain Interview Moves and Epistemic & Register
    groups'. Verify axis keys encode those identifiers explicitly so
    downstream LLM tagger prompts can rely on the canonical names."""
    data = _load_taxonomy()
    axes = data["axes"]

    assert "IV_interview_moves" in axes, "axis IV must be 'IV_interview_moves'"
    assert "V_epistemic_register" in axes, "axis V must be 'V_epistemic_register'"

    iv_groups = set(axes["IV_interview_moves"]["groups"].keys())
    v_groups = set(axes["V_epistemic_register"]["groups"].keys())

    # IV must include genre-defining Interview-Moves groups.
    assert {"Question_Handling", "STAR_Component", "Self_Positioning"} <= iv_groups, (
        f"axis IV missing required Interview-Moves groups: {iv_groups}"
    )
    # V must include the Epistemic + Register calibration groups.
    assert {"Certainty", "Register", "Stance"} <= v_groups, (
        f"axis V missing required Epistemic & Register groups: {v_groups}"
    )
