"""Tests for the overlay layer.

We never touch systemd here — every test passes ``apply_to_systemd=False``
to :class:`OverlayPaths` so file ops can be verified in isolation. The
"applies to systemd correctly" path is covered by the systemd unit tests
in the deploy script + by manual prod verification (it requires a real
systemd, which isn't available in CI).
"""

from __future__ import annotations

import pytest

from orclaw import overlay


@pytest.fixture()
def paths(tmp_path):
    """An overlay rooted under tmp_path, with shipped defaults seeded.

    Mirrors the production layout (defaults_prompts + defaults_systemd
    are separate dirs from the overlay) so we exercise the same
    branching logic.
    """
    defaults_prompts = tmp_path / "shipped" / "prompts"
    defaults_systemd = tmp_path / "shipped" / "systemd"
    defaults_prompts.mkdir(parents=True)
    defaults_systemd.mkdir(parents=True)
    # Seed two default prompts and one default timer + service.
    (defaults_prompts / "implementer-comment.md").write_text("default implementer")
    (defaults_prompts / "reviewer-comment.md").write_text("default reviewer")
    (defaults_systemd / "orclaw-orchestrator.timer").write_text("[Timer]\nOnUnitInactiveSec=30s\n")
    (defaults_systemd / "orclaw-orchestrator.service").write_text("[Service]\nExecStart=/bin/true\n")

    return overlay.OverlayPaths(
        root=tmp_path / "overrides",
        defaults_prompts=defaults_prompts,
        defaults_systemd=defaults_systemd,
        systemd_canonical=tmp_path / "fake-etc",  # never used (apply_to_systemd=False)
        apply_to_systemd=False,
    )


class TestPromptOverlay:
    def test_list_returns_defaults_when_no_overlay(self, paths):
        infos = overlay.list_prompts(paths)
        assert {p.name for p in infos} == {"implementer-comment.md", "reviewer-comment.md"}
        assert all(not p.is_overridden for p in infos)

    def test_read_falls_back_to_default(self, paths):
        content, is_overridden = overlay.read_prompt(paths, "implementer-comment.md")
        assert content == "default implementer"
        assert is_overridden is False

    def test_set_then_read_returns_override(self, paths):
        overlay.set_prompt(paths, "implementer-comment.md", "OVERRIDDEN")
        content, is_overridden = overlay.read_prompt(paths, "implementer-comment.md")
        assert content == "OVERRIDDEN"
        assert is_overridden is True

    def test_list_flags_overridden(self, paths):
        overlay.set_prompt(paths, "reviewer-comment.md", "new reviewer")
        by_name = {p.name: p for p in overlay.list_prompts(paths)}
        assert by_name["reviewer-comment.md"].is_overridden is True
        assert by_name["implementer-comment.md"].is_overridden is False

    def test_delete_override_reverts_to_default(self, paths):
        overlay.set_prompt(paths, "implementer-comment.md", "OVERRIDDEN")
        existed = overlay.delete_prompt_override(paths, "implementer-comment.md")
        assert existed is True
        content, is_overridden = overlay.read_prompt(paths, "implementer-comment.md")
        assert content == "default implementer"
        assert is_overridden is False

    def test_delete_returns_false_when_no_override(self, paths):
        assert overlay.delete_prompt_override(paths, "implementer-comment.md") is False

    def test_read_missing_raises(self, paths):
        with pytest.raises(FileNotFoundError):
            overlay.read_prompt(paths, "does-not-exist.md")


class TestTimerOverlay:
    def test_list_unions_defaults_and_overlay(self, paths):
        overlay.set_timer(paths, "my-custom.timer", "[Timer]\nOnCalendar=hourly\n")
        names = {t.name for t in overlay.list_timers(paths)}
        assert "orclaw-orchestrator.timer" in names
        assert "orclaw-orchestrator.service" in names
        assert "my-custom.timer" in names

    def test_set_marks_overridden_for_default(self, paths):
        overlay.set_timer(paths, "orclaw-orchestrator.timer", "[Timer]\nOnCalendar=*-*-* 00:00\n")
        by_name = {t.name: t for t in overlay.list_timers(paths)}
        assert by_name["orclaw-orchestrator.timer"].is_overridden is True
        assert by_name["orclaw-orchestrator.timer"].is_user_created is False

    def test_set_brand_new_marks_user_created(self, paths):
        overlay.set_timer(paths, "my-custom.timer", "[Timer]\nOnCalendar=hourly\n")
        by_name = {t.name: t for t in overlay.list_timers(paths)}
        assert by_name["my-custom.timer"].is_user_created is True
        assert by_name["my-custom.timer"].is_overridden is True

    def test_read_returns_overlay_then_default(self, paths):
        # default first
        content, info = overlay.read_timer(paths, "orclaw-orchestrator.timer")
        assert "OnUnitInactiveSec=30s" in content
        assert info.is_overridden is False
        # override
        overlay.set_timer(paths, "orclaw-orchestrator.timer", "[Timer]\nOnCalendar=hourly\n")
        content, info = overlay.read_timer(paths, "orclaw-orchestrator.timer")
        assert "OnCalendar=hourly" in content
        assert info.is_overridden is True

    def test_delete_overridden_default_reverts(self, paths):
        overlay.set_timer(paths, "orclaw-orchestrator.timer", "[Timer]\nOnCalendar=hourly\n")
        action = overlay.delete_timer(paths, "orclaw-orchestrator.timer")
        assert action == "reverted"
        # Now reads the default again.
        content, info = overlay.read_timer(paths, "orclaw-orchestrator.timer")
        assert "OnUnitInactiveSec=30s" in content
        assert info.is_overridden is False

    def test_delete_user_created_removes(self, paths):
        overlay.set_timer(paths, "my-custom.timer", "[Timer]\nOnCalendar=hourly\n")
        action = overlay.delete_timer(paths, "my-custom.timer")
        assert action == "removed"
        with pytest.raises(FileNotFoundError):
            overlay.read_timer(paths, "my-custom.timer")

    def test_delete_missing_raises(self, paths):
        with pytest.raises(FileNotFoundError):
            overlay.delete_timer(paths, "nope.timer")

    def test_reject_invalid_unit_name(self, paths):
        with pytest.raises(ValueError):
            overlay.set_timer(paths, "no-extension", "[Timer]\n")
        with pytest.raises(ValueError):
            overlay.set_timer(paths, "wrong.unit", "[Timer]\n")

    def test_reapply_systemd_overrides_no_op_when_empty(self, paths):
        # Nothing in the overlay → returns empty list, never raises.
        assert overlay.reapply_systemd_overrides(paths) == []

    def test_reapply_systemd_overrides_lists_overlay_files(self, paths):
        # We can't verify the cp side effect with apply_to_systemd=False,
        # but the return value should list what *would* be republished.
        overlay.set_timer(paths, "my-custom.timer", "[Timer]\n")
        overlay.set_timer(paths, "another.service", "[Service]\nExecStart=/bin/true\n")
        # apply_to_systemd=False means no _publish_to_systemd call —
        # bypass the side effect by adding files directly.
        refreshed = overlay.reapply_systemd_overrides(paths)
        # Order isn't guaranteed; what matters is content.
        assert set(refreshed) == {"my-custom.timer", "another.service"}
