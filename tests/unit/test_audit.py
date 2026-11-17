from sqlalchemy import text

from app.services.integrity.audit import append_event, verify_chain


def _add(context, count=3):
    with context.sessions() as session:
        for index in range(count):
            append_event(session, actor="t", interface="cli", action="test.event", details={"n": index, "x": "ü"})
        session.commit()


def test_chain_verifies(context):
    _add(context)
    with context.sessions() as session:
        result = verify_chain(session)
    assert result.ok and result.records == 3


def test_edited_record_is_detected(context):
    _add(context)
    with context.sessions() as session:
        session.execute(text("UPDATE audit_events SET action = 'forged' WHERE seq = 2"))
        session.commit()
        result = verify_chain(session)
    assert not result.ok and result.first_bad_seq == 2


def test_deleted_record_is_detected(context):
    _add(context)
    with context.sessions() as session:
        session.execute(text("DELETE FROM audit_events WHERE seq = 2"))
        session.commit()
        result = verify_chain(session)
    assert not result.ok and result.first_bad_seq == 3


def test_concurrent_appends_keep_one_chain(context):
    import threading

    def work():
        for _ in range(10):
            with context.sessions() as session:
                append_event(session, actor="t", interface="cli", action="test.concurrent")
                session.commit()

    threads = [threading.Thread(target=work) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    with context.sessions() as session:
        result = verify_chain(session)
    assert result.ok and result.records == 40
