"""Read-only exact OWN session/marker observations through published state seams."""
from pathlib import Path
from dispatcher.state import SessionRecord,read_session,has_waiting


def observe(fixture):
    state=Path(fixture.state);target=fixture.target;issue=fixture.issue
    paths=[state/f'session-{target}-{issue}',state/f'waiting-{target}-{issue}',state/f'waiting-{issue}']
    artifacts={path.name:{'exists':path.exists(),'content':path.read_bytes() if path.exists() else None} for path in paths}
    return {'session':read_session(state,target,issue),'waiting':has_waiting(state,target,issue),'artifacts':artifacts}


def assert_preserved(case,fixture,before):
    after=observe(fixture)
    case.assertEqual(after['session'],before['session'],'T7_RECEIPT_REWROTE_SESSION_RECORD')
    case.assertEqual(after['waiting'],before['waiting'],'T7_RECEIPT_INVENTED_CURRENT_WAITING_MARKER')
    case.assertEqual(after['artifacts'],before['artifacts'],'T7_RECEIPT_CREATED_OR_REWROTE_SESSION_MARKER_ARTIFACT')
    if before['session'] is not None:case.assertIsInstance(after['session'],SessionRecord)
    return after


def assert_absent(case,fixture):
    state=observe(fixture)
    case.assertIsNone(state['session'])
    case.assertFalse(state['waiting'])
    for artifact in state['artifacts'].values():
        case.assertFalse(artifact['exists'],'T7_MALFORMED_SESSION_MARKER_ARTIFACT_IS_NOT_ABSENCE')
        case.assertIsNone(artifact['content'])
    return state
