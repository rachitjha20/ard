from app.core.resource_guard.guardian import Guardian, Sample


def sample(cpu=10.0, mem=40.0, disk=50_000):
    return Sample("t", cpu, mem, 4000, disk, 100, 1.0)


def make(tmp_path, profile="balanced"):
    return Guardian(tmp_path, profile)


def test_single_spike_does_not_throttle(tmp_path):
    guardian = make(tmp_path)
    guardian.observe(sample(cpu=99))
    guardian.observe(sample(cpu=10))
    assert guardian.state == "normal"


def test_sustained_load_throttles_and_explains(tmp_path):
    guardian = make(tmp_path)
    for _ in range(guardian.profile.sustain_samples):
        guardian.observe(sample(cpu=95))
    assert guardian.state == "throttled"
    assert guardian.worker_limit() == 1
    assert guardian.allows_background()
    explanation = guardian.snapshot()["interventions"][0]["explanation"]
    assert "CPU use is 95%" in explanation


def test_hysteresis_holds_state_between_thresholds(tmp_path):
    guardian = make(tmp_path)
    for _ in range(guardian.profile.sustain_samples):
        guardian.observe(sample(cpu=95))
    # Between recover (60) and high (80): must stay throttled, however long.
    for _ in range(20):
        guardian.observe(sample(cpu=70))
    assert guardian.state == "throttled"
    for _ in range(guardian.profile.sustain_samples):
        guardian.observe(sample(cpu=20))
    assert guardian.state == "normal"


def test_critical_memory_or_low_disk_pauses_background_only(tmp_path):
    guardian = make(tmp_path)
    for _ in range(guardian.profile.sustain_samples):
        guardian.observe(sample(mem=96))
    assert guardian.state == "paused"
    assert not guardian.allows_background()
    assert guardian.worker_limit() >= 1  # interactive work is still possible

    other = make(tmp_path)
    for _ in range(other.profile.sustain_samples):
        other.observe(sample(disk=10))
    assert other.state == "paused"


def test_profiles(tmp_path):
    guardian = make(tmp_path, "eco")
    assert guardian.worker_limit() == 1
    guardian.set_profile("custom", {"cpu_high": 50, "cpu_recover": 30, "max_workers": 1})
    assert guardian.profile.cpu_high == 50
    import pytest
    with pytest.raises(ValueError):
        guardian.set_profile("custom", {"cpu_high": 20, "cpu_recover": 30})
    with pytest.raises(ValueError):
        guardian.set_profile("turbo")


def test_guardian_has_no_destructive_capabilities():
    """It must never kill processes, shut down or delete: check the source for such calls."""
    import inspect
    import app.core.resource_guard.guardian as module

    source = inspect.getsource(module)
    for forbidden in (".kill(", ".terminate(", "shutdown", "os.remove", "unlink", "rmtree", "os.system", "subprocess"):
        assert forbidden not in source, forbidden
