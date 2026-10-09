"""A stopped observation alone never grants physical-close authority."""
import pytest

from tests.test_t4_runtime_acceptance import world, dispatcher_fixture  # noqa: F401


def test_stopped_launch_without_stage_signal_remains_open(world, monkeypatch):
    binding = world.prepare(ticket='')
    world.stopped(binding)
    run, load, external = dispatcher_fixture(world, monkeypatch, binding)
    (world.worktree / '.agent' / 'stage.json').unlink()
    run()
    assert load().park == ''
    assert not world.view(binding)['retired']
    assert not any(item[0] in {'close', 'podman.rm'} for item in external)
    assert world.host.accept_input(binding, 'next-prompt')


@pytest.mark.parametrize("stage,ticket", [("review", ""), ("implement", "1")])
def test_stop_from_previous_stage_cannot_park_current_implementation(world, monkeypatch, stage, ticket):
    import json
    from dataclasses import replace
    from dispatcher.state import Stage, save
    binding = world.prepare(ticket=ticket, stage=stage)
    world.stopped(binding)
    run, load, external = dispatcher_fixture(world, monkeypatch, binding)
    save(world.state, replace(load(), stage=Stage.IMPLEMENT, ticket_cursor=2))
    (world.worktree / '.agent' / 'stage.json').write_text(json.dumps({'stage': 'implement', 'status': 'working'}))
    run()
    assert load().park == ''
    assert not world.view(binding)['retired']
    assert not any(item[0] in {'close', 'podman.rm'} for item in external)


@pytest.mark.parametrize('status,expected', [('working', 'failed'), ('done', 'pr-open')])
def test_dead_bound_service_overrides_live_terminal_without_overriding_done(world, monkeypatch, status, expected):
    binding = world.prepare(ticket='')
    world.stopped(binding)
    assert world.host.event(binding, {'type': 'service', 'status': 'dead'})
    run, load, external = dispatcher_fixture(world, monkeypatch, binding, status=status)
    run()
    assert load().stage.value == expected
    if status == 'working':
        closures = [item[1] for item in external if item[0] in {'close', 'podman.rm'}]
        assert closures and all(snapshot['retired'] for snapshot in closures)
