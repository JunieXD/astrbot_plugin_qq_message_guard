from dataclasses import replace

from conftest import ADMIN


async def test_old_observation_records_do_not_starve_current_notifications(env):
    await env.speak("old observation")
    old = await env.process()
    # More history than one notification fetch, without invoking the model repeatedly.
    await env.store.db.executemany(
        "INSERT INTO cases(event_key,scope,created,updated,state,flagged,notified,human,settings_revision,data)"
        " VALUES(?,?,?,?,?,1,0,'',?,?)",
        [
            (
                f"history-{i}",
                old["scope"],
                old["created"],
                old["updated"],
                "review",
                old["settings_revision"],
                "{}",
            )
            for i in range(1100)
        ],
    )
    await env.store.db.commit()
    env.configure(notify_private=True)
    env.box.settings = replace(env.box.settings, notify_users=(ADMIN,))
    await env.speak("new actionable notification")
    new = await env.process()
    await env.s.send_alerts()
    assert len(env.bot.writes) == 1
    assert (await env.store.call("case", new["id"]))["notified"]
    assert not (await env.store.call("case", old["id"]))["notified"]


async def test_active_action_query_has_index_and_does_not_decode_review_history(env):
    await env.speak()
    await env.process()
    plan = await env.store.call(
        "_rows",
        "EXPLAIN QUERY PLAN SELECT * FROM cases WHERE action_ready=1"
        " AND state='review' AND human='' ORDER BY created,id LIMIT 1",
    )
    assert any("case_actions" in row["detail"] for row in plan)
    assert await env.store.call("next_recall") is None
